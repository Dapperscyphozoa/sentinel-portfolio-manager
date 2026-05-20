// poly_signer/src/submit.rs
//
// Submits signed orders to the Polymarket CLOB REST API.
//
// PM CLOB requires two layers of auth:
//   1. EIP-712 signature on the Order struct (L1 — wallet identity)
//   2. POLY HMAC signature on the request body (L2 — API key identity)
//
// The L2 HMAC binding format (verify against current PM docs before deploy):
//   message = base64(HMAC-SHA256(api_secret_base64, timestamp + method + path + body))
//
// Endpoint: POST https://clob.polymarket.com/order
// Required headers:
//   POLY_ADDRESS      — 0x-prefixed wallet address
//   POLY_SIGNATURE    — base64 of the L2 HMAC
//   POLY_TIMESTAMP    — unix seconds
//   POLY_API_KEY      — your API key
//   POLY_PASSPHRASE   — your API passphrase

use anyhow::{Context, Result};
use base64::{engine::general_purpose::STANDARD as B64, Engine};
use ethers::core::types::{Address, Signature};
use hmac::{Hmac, Mac};
use serde::Serialize;
use serde_json::json;
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::eip712::PolymarketOrder;

const CLOB_BASE: &str = "https://clob.polymarket.com";
const PATH_ORDER: &str = "/order";
const PATH_CANCEL: &str = "/order/cancel";

pub struct ApiCreds {
    pub key: String,
    pub secret: String,
    pub passphrase: String,
}

pub struct SubmitResult {
    pub status: SubmitStatus,
    pub pm_order_id: Option<String>,
    pub fill_amount: Option<f64>,
    pub raw: serde_json::Value,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SubmitStatus {
    Posted,
    Filled,
    PartialFill,
    Rejected,
    Error,
}

pub async fn submit_order(
    client: &reqwest::Client,
    order: &PolymarketOrder,
    signature: &Signature,
    maker: Address,
    creds: &ApiCreds,
    order_type: &str,            // "GTC" | "FOK" | "GTD"
) -> Result<SubmitResult> {
    let body = json!({
        "order": {
            "salt": order.salt.to_string(),
            "maker": format!("0x{:x}", order.maker),
            "signer": format!("0x{:x}", order.signer),
            "taker": format!("0x{:x}", order.taker),
            "tokenId": order.token_id.to_string(),
            "makerAmount": order.maker_amount.to_string(),
            "takerAmount": order.taker_amount.to_string(),
            "expiration": order.expiration.to_string(),
            "nonce": order.nonce.to_string(),
            "feeRateBps": order.fee_rate_bps.to_string(),
            "side": if order.side == 0 { "BUY" } else { "SELL" },
            "signatureType": order.signature_type,
            "signature": format!("0x{}", hex::encode(signature.to_vec())),
        },
        "owner": format!("0x{:x}", maker),
        "orderType": order_type,
    });

    let body_str = serde_json::to_string(&body)?;
    let url = format!("{}{}", CLOB_BASE, PATH_ORDER);
    let headers = build_l2_headers(creds, "POST", PATH_ORDER, &body_str, maker)?;

    let mut rb = client.post(&url);
    for (k, v) in headers {
        rb = rb.header(k, v);
    }
    let resp = rb
        .header("content-type", "application/json")
        .body(body_str)
        .send()
        .await
        .context("POST /order")?;

    let status_code = resp.status();
    let resp_json: serde_json::Value = resp
        .json()
        .await
        .unwrap_or_else(|_| serde_json::Value::Null);

    if !status_code.is_success() {
        return Ok(SubmitResult {
            status: SubmitStatus::Rejected,
            pm_order_id: None,
            fill_amount: None,
            raw: resp_json.clone(),
            error: Some(format!("http {}", status_code)),
        });
    }

    let pm_status = resp_json
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_lowercase();
    let mapped = match pm_status.as_str() {
        "matched" => SubmitStatus::Filled,
        "delayed" | "live" => SubmitStatus::Posted,
        _ => SubmitStatus::Rejected,
    };
    let making_amount = resp_json
        .get("makingAmount")
        .and_then(|v| v.as_str())
        .and_then(|s| s.parse::<f64>().ok())
        .map(|v| v / 1_000_000.0);
    let pm_order_id = resp_json
        .get("orderID")
        .or(resp_json.get("orderId"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    Ok(SubmitResult {
        status: mapped,
        pm_order_id,
        fill_amount: making_amount,
        raw: resp_json,
        error: None,
    })
}

pub async fn cancel_order(
    client: &reqwest::Client,
    order_id: &str,
    maker: Address,
    creds: &ApiCreds,
) -> Result<serde_json::Value> {
    let body = json!({ "orderID": order_id });
    let body_str = serde_json::to_string(&body)?;
    let url = format!("{}{}", CLOB_BASE, PATH_CANCEL);
    let headers = build_l2_headers(creds, "DELETE", PATH_CANCEL, &body_str, maker)?;
    let mut rb = client.delete(&url);
    for (k, v) in headers {
        rb = rb.header(k, v);
    }
    let resp = rb
        .header("content-type", "application/json")
        .body(body_str)
        .send()
        .await
        .context("DELETE /order/cancel")?;
    Ok(resp.json().await.unwrap_or_else(|_| serde_json::Value::Null))
}

fn build_l2_headers(
    creds: &ApiCreds,
    method: &str,
    path: &str,
    body: &str,
    maker: Address,
) -> Result<Vec<(String, String)>> {
    let ts = SystemTime::now()
        .duration_since(UNIX_EPOCH)?
        .as_secs()
        .to_string();

    // PM's L2 HMAC: base64(HMAC-SHA256(b64_decode(secret), ts + method + path + body))
    let secret_bytes = B64
        .decode(creds.secret.as_bytes())
        .or_else(|_| Ok::<_, anyhow::Error>(creds.secret.as_bytes().to_vec()))?;
    let mut mac = Hmac::<Sha256>::new_from_slice(&secret_bytes)
        .context("hmac key length")?;
    let msg = format!("{}{}{}{}", ts, method, path, body);
    mac.update(msg.as_bytes());
    let sig = B64.encode(mac.finalize().into_bytes());

    Ok(vec![
        ("POLY_ADDRESS".to_string(), format!("0x{:x}", maker)),
        ("POLY_SIGNATURE".to_string(), sig),
        ("POLY_TIMESTAMP".to_string(), ts),
        ("POLY_API_KEY".to_string(), creds.key.clone()),
        ("POLY_PASSPHRASE".to_string(), creds.passphrase.clone()),
    ])
}
