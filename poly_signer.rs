// poly_signer/src/main.rs
//
// Polymarket CTF Exchange order signer microservice.
//
// Architecture:
//   poly-runner (Python) ──Unix socket──> poly-signer (Rust) ──HTTPS──> Polymarket CLOB API
//
// Why Rust:
//   - EIP-712 signing in pure Python: ~10-20ms (eth_account)
//   - Same signing in Rust (ethers-rs + k256): <1ms
//   - HTTP POST round-trip dominates remaining latency, but pre-signing speed compounds
//     when running many quotes per second (maker_quote does ~4/sec/market * 5 markets = 20/sec)
//
// Build:
//   cd poly_signer && cargo build --release
//   target/release/poly-signer
//
// Required env vars:
//   POLY_PRIVATE_KEY      — hex private key, 0x-prefixed
//   POLY_API_KEY          — Polymarket API key (from PM dashboard)
//   POLY_API_SECRET       — Polymarket API secret
//   POLY_API_PASSPHRASE   — Polymarket API passphrase
//   POLY_CHAIN_ID         — 137 (Polygon mainnet)
//   POLY_EXCHANGE_ADDR    — Polymarket CTF Exchange contract (verify on Polygonscan)
//   SOCKET_PATH           — default /tmp/poly-signer.sock

use ethers::{
    core::types::{transaction::eip712::Eip712, Address, U256},
    signers::{LocalWallet, Signer},
};
use serde::{Deserialize, Serialize};
use std::{
    error::Error,
    os::unix::net::{UnixListener, UnixStream},
    io::{Read, Write},
    sync::Arc,
};
use tokio::runtime::Runtime;

// ───────────────────────────────────────────────────────────────────────────────
// Wire types: runner → signer

#[derive(Deserialize, Debug)]
struct OrderRequest {
    market_id: String,           // PM market UUID
    token_id: String,            // YES or NO token ID (uint256 as string)
    side: OrderSide,
    price: f64,                  // 0.01 to 0.99
    size_usdc: f64,              // notional USDC.e
    expiration: u64,             // unix seconds; 0 = no expiry (use with caution)
    nonce: u64,                  // monotonic per maker; runner manages
    order_type: OrderType,       // GTC (post limit) or FOK (take immediately)
    client_order_id: String,     // for runner-side correlation
}

#[derive(Deserialize, Debug, Clone, Copy)]
enum OrderSide { Buy, Sell }

#[derive(Deserialize, Debug, Clone, Copy)]
enum OrderType { Gtc, Fok }

#[derive(Serialize, Debug)]
struct OrderResponse {
    client_order_id: String,
    order_hash: String,
    status: OrderStatus,
    fill_amount: Option<f64>,
    fill_price: Option<f64>,
    error: Option<String>,
    signing_ms: u64,
    total_ms: u64,
}

#[derive(Serialize, Debug)]
enum OrderStatus { Posted, Filled, PartialFill, Rejected, Error }

// ───────────────────────────────────────────────────────────────────────────────
// EIP-712 types — Polymarket CTF Exchange Order
//
// Verify this schema against current PM CLOB docs before mainnet:
//   https://docs.polymarket.com/?python#signing-orders
//
// As of writing the Order struct contains:
//   salt, maker, signer, taker, tokenId, makerAmount, takerAmount,
//   expiration, nonce, feeRateBps, side, signatureType

use ethers::contract::EthAbiType;
use ethers_derive_eip712::*;

#[derive(Debug, Clone, Eip712, EthAbiType, Serialize, Deserialize)]
#[eip712(
    name = "Polymarket CTF Exchange",
    version = "1",
    chain_id = 137,
    verifying_contract = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    // ↑ VERIFY ON POLYGONSCAN before mainnet trade
)]
struct PolymarketOrder {
    salt: U256,
    maker: Address,
    signer: Address,
    taker: Address,       // address(0) for public order
    token_id: U256,
    maker_amount: U256,   // 6 decimals for USDC.e (collateral side)
    taker_amount: U256,   // 6 decimals for CTF outcome token
    expiration: U256,
    nonce: U256,
    fee_rate_bps: U256,
    side: u8,             // 0 = BUY, 1 = SELL
    signature_type: u8,   // 0 = EOA, 1 = POLY_PROXY, 2 = POLY_GNOSIS_SAFE
}

// ───────────────────────────────────────────────────────────────────────────────
// Builder — runner-friendly inputs → on-chain typed struct

fn build_order(
    req: &OrderRequest,
    maker_address: Address,
) -> Result<PolymarketOrder, Box<dyn Error>> {
    // Convert price (0.01..=0.99) + size_usdc to maker_amount / taker_amount
    // For BUY at price p with $S notional:
    //   maker_amount = S * 1e6 (USDC.e, 6 decimals)
    //   taker_amount = (S / p) * 1e6 (outcome tokens, 6 decimals)
    // For SELL at price p with $S notional:
    //   maker_amount = (S / p) * 1e6 (outcome tokens, 6 decimals)
    //   taker_amount = S * 1e6 (USDC.e)
    
    let usdc_units = (req.size_usdc * 1e6) as u128;
    let token_units = ((req.size_usdc / req.price) * 1e6) as u128;
    
    let (maker_amount, taker_amount, side) = match req.side {
        OrderSide::Buy => (
            U256::from(usdc_units),
            U256::from(token_units),
            0u8,
        ),
        OrderSide::Sell => (
            U256::from(token_units),
            U256::from(usdc_units),
            1u8,
        ),
    };
    
    // Salt: random uint256 for replay protection
    let salt = U256::from(rand::random::<u128>());
    
    let token_id = U256::from_dec_str(&req.token_id)?;
    
    Ok(PolymarketOrder {
        salt,
        maker: maker_address,
        signer: maker_address,
        taker: Address::zero(),       // public order
        token_id,
        maker_amount,
        taker_amount,
        expiration: U256::from(req.expiration),
        nonce: U256::from(req.nonce),
        fee_rate_bps: U256::zero(),   // signer side fee; PM derives actual fee dynamically
        side,
        signature_type: 0,            // EOA — change to 1 if using Polymarket proxy wallet
    })
}

// ───────────────────────────────────────────────────────────────────────────────
// Signing + submission

async fn sign_and_submit(
    req: OrderRequest,
    wallet: Arc<LocalWallet>,
    client: Arc<reqwest::Client>,
    api_key: &str,
    api_secret: &str,
    api_passphrase: &str,
) -> OrderResponse {
    let start = std::time::Instant::now();
    let maker_address = wallet.address();
    
    // Build typed struct
    let order = match build_order(&req, maker_address) {
        Ok(o) => o,
        Err(e) => return OrderResponse {
            client_order_id: req.client_order_id,
            order_hash: String::new(),
            status: OrderStatus::Error,
            fill_amount: None,
            fill_price: None,
            error: Some(format!("build_order: {}", e)),
            signing_ms: 0,
            total_ms: start.elapsed().as_millis() as u64,
        },
    };
    
    // Sign EIP-712
    let sign_start = std::time::Instant::now();
    let signature = match wallet.sign_typed_data(&order).await {
        Ok(sig) => sig,
        Err(e) => return OrderResponse {
            client_order_id: req.client_order_id,
            order_hash: String::new(),
            status: OrderStatus::Error,
            fill_amount: None,
            fill_price: None,
            error: Some(format!("sign: {}", e)),
            signing_ms: sign_start.elapsed().as_millis() as u64,
            total_ms: start.elapsed().as_millis() as u64,
        },
    };
    let signing_ms = sign_start.elapsed().as_millis() as u64;
    
    let order_hash = order.encode_eip712()
        .map(|h| format!("0x{}", hex::encode(h)))
        .unwrap_or_default();
    
    // POST to PM CLOB API
    // Endpoint: POST https://clob.polymarket.com/order
    // Headers: POLY_ADDRESS, POLY_SIGNATURE, POLY_TIMESTAMP, POLY_API_KEY, POLY_PASSPHRASE
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs()
        .to_string();
    
    let body = serde_json::json!({
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
        "owner": format!("0x{:x}", maker_address),
        "orderType": match req.order_type {
            OrderType::Gtc => "GTC",
            OrderType::Fok => "FOK",
        },
    });
    
    // POLY HMAC for L2 auth (separate from EIP-712):
    //   L2 signature = HMAC_SHA256(api_secret, timestamp + method + path + body)
    let l2_message = format!("{}{}{}{}",  timestamp, "POST", "/order", body.to_string());
    let l2_sig = hmac_sha256(api_secret.as_bytes(), l2_message.as_bytes());
    
    let resp = client
        .post("https://clob.polymarket.com/order")
        .header("POLY_ADDRESS", format!("0x{:x}", maker_address))
        .header("POLY_SIGNATURE", base64::encode(&l2_sig))
        .header("POLY_TIMESTAMP", &timestamp)
        .header("POLY_API_KEY", api_key)
        .header("POLY_PASSPHRASE", api_passphrase)
        .header("content-type", "application/json")
        .json(&body)
        .send()
        .await;
    
    let total_ms = start.elapsed().as_millis() as u64;
    
    match resp {
        Ok(r) if r.status().is_success() => {
            let body_json: serde_json::Value = r.json().await.unwrap_or_default();
            let status = match body_json["status"].as_str() {
                Some("matched") => OrderStatus::Filled,
                Some("delayed") => OrderStatus::Posted,
                Some("live") => OrderStatus::Posted,
                _ => OrderStatus::Rejected,
            };
            OrderResponse {
                client_order_id: req.client_order_id,
                order_hash,
                status,
                fill_amount: body_json["makingAmount"].as_str().and_then(|s| s.parse::<f64>().ok()).map(|v| v / 1e6),
                fill_price: None,  // PM API returns this in fill events, not order ack
                error: None,
                signing_ms,
                total_ms,
            }
        }
        Ok(r) => OrderResponse {
            client_order_id: req.client_order_id,
            order_hash,
            status: OrderStatus::Rejected,
            fill_amount: None,
            fill_price: None,
            error: Some(format!("http {}: {}", r.status(), r.text().await.unwrap_or_default())),
            signing_ms,
            total_ms,
        },
        Err(e) => OrderResponse {
            client_order_id: req.client_order_id,
            order_hash,
            status: OrderStatus::Error,
            fill_amount: None,
            fill_price: None,
            error: Some(format!("submit: {}", e)),
            signing_ms,
            total_ms,
        },
    }
}

fn hmac_sha256(key: &[u8], msg: &[u8]) -> Vec<u8> {
    use hmac::{Hmac, Mac};
    use sha2::Sha256;
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("hmac key");
    mac.update(msg);
    mac.finalize().into_bytes().to_vec()
}

// ───────────────────────────────────────────────────────────────────────────────
// Unix socket server

fn main() -> Result<(), Box<dyn Error>> {
    let private_key = std::env::var("POLY_PRIVATE_KEY")?;
    let chain_id: u64 = std::env::var("POLY_CHAIN_ID").unwrap_or("137".into()).parse()?;
    let api_key = std::env::var("POLY_API_KEY")?;
    let api_secret = std::env::var("POLY_API_SECRET")?;
    let api_passphrase = std::env::var("POLY_API_PASSPHRASE")?;
    let socket_path = std::env::var("SOCKET_PATH").unwrap_or("/tmp/poly-signer.sock".into());
    
    let wallet: LocalWallet = private_key.parse::<LocalWallet>()?.with_chain_id(chain_id);
    let wallet = Arc::new(wallet);
    let client = Arc::new(reqwest::Client::builder()
        .timeout(std::time::Duration::from_millis(2000))
        .build()?);
    
    let _ = std::fs::remove_file(&socket_path);
    let listener = UnixListener::bind(&socket_path)?;
    eprintln!("poly-signer listening on {}", socket_path);
    
    let runtime = Runtime::new()?;
    
    for stream in listener.incoming() {
        let stream = stream?;
        let wallet = wallet.clone();
        let client = client.clone();
        let api_key = api_key.clone();
        let api_secret = api_secret.clone();
        let api_passphrase = api_passphrase.clone();
        
        runtime.spawn(async move {
            if let Err(e) = handle_client(stream, wallet, client, &api_key, &api_secret, &api_passphrase).await {
                eprintln!("client error: {}", e);
            }
        });
    }
    Ok(())
}

async fn handle_client(
    mut stream: UnixStream,
    wallet: Arc<LocalWallet>,
    client: Arc<reqwest::Client>,
    api_key: &str,
    api_secret: &str,
    api_passphrase: &str,
) -> Result<(), Box<dyn Error>> {
    let mut buf = String::new();
    stream.read_to_string(&mut buf)?;
    let req: OrderRequest = serde_json::from_str(&buf)?;
    
    let resp = sign_and_submit(req, wallet, client, api_key, api_secret, api_passphrase).await;
    let out = serde_json::to_string(&resp)?;
    stream.write_all(out.as_bytes())?;
    Ok(())
}

// ───────────────────────────────────────────────────────────────────────────────
// Cargo.toml (companion file)
//
// [package]
// name = "poly-signer"
// version = "0.1.0"
// edition = "2021"
//
// [dependencies]
// ethers = { version = "2", features = ["eip712", "abigen"] }
// ethers-derive-eip712 = "1"
// tokio = { version = "1", features = ["full"] }
// serde = { version = "1", features = ["derive"] }
// serde_json = "1"
// reqwest = { version = "0.12", features = ["json", "rustls-tls"] }
// hmac = "0.12"
// sha2 = "0.10"
// base64 = "0.22"
// hex = "0.4"
// rand = "0.8"
//
// [profile.release]
// lto = true
// codegen-units = 1
// strip = true
