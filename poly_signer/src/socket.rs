// poly_signer/src/socket.rs
//
// Unix domain socket server. Listens at $POLY_SIGNER_SOCKET (default
// /tmp/poly-signer.sock), spawns one tokio task per incoming connection,
// reads one JSON request, returns one JSON response, closes the socket.
//
// Wire schema mirrors common/poly_signer_client.py.

use anyhow::{Context, Result};
use ethers::core::types::{Address, Signature, U256};
use ethers::signers::{LocalWallet, Signer};
use ethers::types::transaction::eip712::Eip712;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Instant;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};

use crate::eip712::{build_order, PolymarketOrder, Side, SignatureType};
use crate::submit::{cancel_order, submit_order, ApiCreds, SubmitStatus};

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum WireRequest {
    Order(OrderRequest),
    Action(ActionRequest),
}

#[derive(Debug, Deserialize)]
struct OrderRequest {
    market_id: String,
    token_id: String,
    side: String,             // "Buy" | "Sell"
    price: f64,
    size_usdc: f64,
    expiration: u64,
    nonce: u64,
    order_type: String,       // "Gtc" | "Fok"
    client_order_id: String,
}

#[derive(Debug, Deserialize)]
struct ActionRequest {
    action: String,
    order_id: Option<String>,
}

#[derive(Debug, Serialize)]
struct OrderResponse {
    client_order_id: String,
    order_hash: String,
    status: String,
    fill_amount: Option<f64>,
    fill_price: Option<f64>,
    error: Option<String>,
    signing_ms: u64,
    total_ms: u64,
}

#[derive(Clone)]
pub struct SignerCtx {
    pub wallet: Arc<LocalWallet>,
    pub creds: Arc<ApiCreds>,
    pub maker: Address,
    pub sig_type: SignatureType,
    pub http: reqwest::Client,
    pub dry_run: bool,
}

pub async fn serve(socket_path: &str, ctx: SignerCtx) -> Result<()> {
    if Path::new(socket_path).exists() {
        std::fs::remove_file(socket_path).ok();
    }
    let listener = UnixListener::bind(socket_path)
        .with_context(|| format!("bind {}", socket_path))?;
    tracing::info!("poly-signer listening on {}", socket_path);

    loop {
        let (stream, _) = listener.accept().await?;
        let ctx = ctx.clone();
        tokio::spawn(async move {
            if let Err(e) = handle_conn(stream, ctx).await {
                tracing::warn!("conn error: {:#}", e);
            }
        });
    }
}

async fn handle_conn(mut stream: UnixStream, ctx: SignerCtx) -> Result<()> {
    let mut raw = String::new();
    stream.read_to_string(&mut raw).await?;
    if raw.is_empty() {
        return Ok(());
    }

    let req: WireRequest = serde_json::from_str(&raw)
        .with_context(|| format!("parse request: {}", raw))?;

    let resp_json = match req {
        WireRequest::Order(o) => handle_order(o, &ctx).await,
        WireRequest::Action(a) => handle_action(a, &ctx).await,
    };

    let body = serde_json::to_vec(&resp_json)?;
    stream.write_all(&body).await?;
    stream.shutdown().await.ok();
    Ok(())
}

async fn handle_order(o: OrderRequest, ctx: &SignerCtx) -> serde_json::Value {
    let t0 = Instant::now();
    let side = match o.side.as_str() {
        "Buy" => Side::Buy,
        "Sell" => Side::Sell,
        s => {
            return error_resp(&o.client_order_id, format!("bad side: {}", s),
                              0, t0.elapsed().as_millis() as u64);
        }
    };
    let token_id = match U256::from_dec_str(&o.token_id) {
        Ok(v) => v,
        Err(e) => {
            return error_resp(&o.client_order_id, format!("bad token_id: {}", e),
                              0, t0.elapsed().as_millis() as u64);
        }
    };

    // Polymarket fee schedule (passed to contract for accounting; PM may
    // override this server-side based on its dynamic-fee curve).
    let fee_rate_bps: u64 = 0;

    let order = match build_order(
        side,
        o.price,
        o.size_usdc,
        token_id,
        ctx.maker,
        o.expiration,
        o.nonce,
        fee_rate_bps,
        ctx.sig_type,
    ) {
        Ok(o) => o,
        Err(e) => {
            return error_resp(&o.client_order_id, e.to_string(),
                              0, t0.elapsed().as_millis() as u64);
        }
    };

    // EIP-712 sign
    let t_sign = Instant::now();
    let sig: Signature = match sign_typed(&order, &ctx.wallet).await {
        Ok(s) => s,
        Err(e) => {
            return error_resp(&o.client_order_id, format!("sign failed: {}", e),
                              0, t0.elapsed().as_millis() as u64);
        }
    };
    let signing_ms = t_sign.elapsed().as_millis() as u64;

    let order_hash = match order.encode_eip712() {
        Ok(h) => format!("0x{}", hex::encode(h)),
        Err(_) => "".to_string(),
    };

    if ctx.dry_run {
        return serde_json::to_value(OrderResponse {
            client_order_id: o.client_order_id,
            order_hash,
            status: "Posted".to_string(),
            fill_amount: None,
            fill_price: None,
            error: Some("DRY_RUN".to_string()),
            signing_ms,
            total_ms: t0.elapsed().as_millis() as u64,
        }).unwrap();
    }

    let order_type_str = if o.order_type == "Fok" { "FOK" } else { "GTC" };

    let result = match submit_order(&ctx.http, &order, &sig, ctx.maker,
                                     &ctx.creds, order_type_str).await {
        Ok(r) => r,
        Err(e) => {
            return error_resp(&o.client_order_id, format!("submit failed: {}", e),
                              signing_ms, t0.elapsed().as_millis() as u64);
        }
    };

    let status_str = match result.status {
        SubmitStatus::Posted => "Posted",
        SubmitStatus::Filled => "Filled",
        SubmitStatus::PartialFill => "PartialFill",
        SubmitStatus::Rejected => "Rejected",
        SubmitStatus::Error => "Error",
    };

    serde_json::to_value(OrderResponse {
        client_order_id: o.client_order_id,
        order_hash,
        status: status_str.to_string(),
        fill_amount: result.fill_amount,
        fill_price: Some(o.price),
        error: result.error,
        signing_ms,
        total_ms: t0.elapsed().as_millis() as u64,
    }).unwrap()
}

async fn handle_action(a: ActionRequest, ctx: &SignerCtx) -> serde_json::Value {
    match a.action.as_str() {
        "cancel" => {
            let id = match a.order_id {
                Some(i) => i,
                None => return serde_json::json!({"error": "missing order_id"}),
            };
            match cancel_order(&ctx.http, &id, ctx.maker, &ctx.creds).await {
                Ok(j) => j,
                Err(e) => serde_json::json!({"error": e.to_string()}),
            }
        }
        other => serde_json::json!({"error": format!("unknown action: {}", other)}),
    }
}

async fn sign_typed(order: &PolymarketOrder, wallet: &LocalWallet) -> Result<Signature> {
    let sig = wallet.sign_typed_data(order).await?;
    Ok(sig)
}

fn error_resp(cloid: &str, err: String, signing_ms: u64, total_ms: u64)
    -> serde_json::Value
{
    serde_json::to_value(OrderResponse {
        client_order_id: cloid.to_string(),
        order_hash: "".to_string(),
        status: "Error".to_string(),
        fill_amount: None,
        fill_price: None,
        error: Some(err),
        signing_ms,
        total_ms,
    }).unwrap()
}
