// poly_signer/src/main.rs
//
// Entry point. Loads env, constructs LocalWallet from POLY_PRIVATE_KEY,
// reads PM API creds, then runs the Unix-socket server.
//
// Env vars (all required for live; minus the keys for dry-run):
//   POLY_PRIVATE_KEY     32-byte hex (with or without 0x prefix)
//   POLY_MAKER_ADDRESS   wallet OR PM proxy address (if SIG_TYPE=1)
//   POLY_SIG_TYPE        "eoa" | "proxy" | "safe"   (default: proxy)
//   POLY_API_KEY         PM L2 API key
//   POLY_API_SECRET      PM L2 API secret (base64)
//   POLY_API_PASSPHRASE  PM L2 API passphrase
//   POLY_SIGNER_SOCKET   path to bind  (default: /tmp/poly-signer.sock)
//   POLY_DRY_RUN         "1" to skip network POST (sign+return Posted)

mod eip712;
mod submit;
mod socket;

use anyhow::{Context, Result};
use ethers::core::types::Address;
use ethers::signers::LocalWallet;
use std::env;
use std::str::FromStr;
use std::sync::Arc;

use crate::eip712::SignatureType;
use crate::socket::{serve, SignerCtx};
use crate::submit::ApiCreds;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env()
            .add_directive("info".parse().unwrap()))
        .init();

    let dry_run = env::var("POLY_DRY_RUN").unwrap_or_default() == "1";

    // Wallet
    let pk_raw = env::var("POLY_PRIVATE_KEY")
        .context("POLY_PRIVATE_KEY env var required")?;
    let pk = pk_raw.trim_start_matches("0x");
    let wallet: LocalWallet = pk.parse::<LocalWallet>()
        .context("parse private key")?;

    // Maker address (proxy or EOA)
    let sig_type_raw = env::var("POLY_SIG_TYPE").unwrap_or_else(|_| "proxy".into());
    let sig_type = match sig_type_raw.to_lowercase().as_str() {
        "eoa" => SignatureType::Eoa,
        "proxy" | "polyproxy" => SignatureType::PolyProxy,
        "safe" | "gnosis" => SignatureType::PolyGnosisSafe,
        other => anyhow::bail!("bad POLY_SIG_TYPE: {}", other),
    };

    let maker = match env::var("POLY_MAKER_ADDRESS") {
        Ok(addr) => Address::from_str(addr.trim_start_matches("0x"))
            .context("parse POLY_MAKER_ADDRESS")?,
        Err(_) => {
            // Default to the wallet address (only valid for EOA signature_type)
            use ethers::signers::Signer;
            wallet.address()
        }
    };

    // PM API creds
    let creds = if dry_run {
        ApiCreds {
            key: env::var("POLY_API_KEY").unwrap_or_default(),
            secret: env::var("POLY_API_SECRET").unwrap_or_default(),
            passphrase: env::var("POLY_API_PASSPHRASE").unwrap_or_default(),
        }
    } else {
        ApiCreds {
            key: env::var("POLY_API_KEY").context("POLY_API_KEY")?,
            secret: env::var("POLY_API_SECRET").context("POLY_API_SECRET")?,
            passphrase: env::var("POLY_API_PASSPHRASE").context("POLY_API_PASSPHRASE")?,
        }
    };

    let http = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .pool_max_idle_per_host(8)
        .build()?;

    let ctx = SignerCtx {
        wallet: Arc::new(wallet),
        creds: Arc::new(creds),
        maker,
        sig_type,
        http,
        dry_run,
    };

    let socket_path = env::var("POLY_SIGNER_SOCKET")
        .unwrap_or_else(|_| "/tmp/poly-signer.sock".to_string());

    tracing::info!(
        "poly-signer starting (maker={:?}, sig_type={:?}, dry_run={})",
        ctx.maker, sig_type_raw, ctx.dry_run
    );

    serve(&socket_path, ctx).await
}
