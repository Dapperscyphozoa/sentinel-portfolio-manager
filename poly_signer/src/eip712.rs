// poly_signer/src/eip712.rs
//
// EIP-712 typed-data schema for the Polymarket CTF Exchange `Order` struct.
//
// The struct fields and domain separator MUST be verified against the
// current Polymarket CLOB documentation before mainnet trading.
// Reference: https://docs.polymarket.com/?python#signing-orders
//
// As of writing, the verifying contract addresses are:
//   Polygon Mainnet (CTF Exchange):
//     0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E    ← VERIFY ON POLYGONSCAN
//   Neg-Risk CTF Exchange (binary markets):
//     0xC5d563A36AE78145C45a50134d48A1215220f80a    ← VERIFY
//
// Signature types (SignatureType enum):
//   0 = EOA           (plain externally-owned wallet)
//   1 = POLY_PROXY    (Polymarket proxy wallet — gas-abstracted)
//   2 = POLY_GNOSIS_SAFE
//
// If the operator funded a Polymarket proxy wallet via the standard PM
// deposit flow, signature_type = 1 and `maker` = the proxy address.
// If using a raw EOA, signature_type = 0 and `maker` = wallet address.

use ethers::contract::EthAbiType;
use ethers::core::types::{Address, U256};
use ethers_derive_eip712::*;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Eip712, EthAbiType, Serialize, Deserialize)]
#[eip712(
    name = "Polymarket CTF Exchange",
    version = "1",
    chain_id = 137,
    verifying_contract = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
)]
pub struct PolymarketOrder {
    pub salt: U256,
    pub maker: Address,
    pub signer: Address,
    pub taker: Address,
    pub token_id: U256,
    pub maker_amount: U256,
    pub taker_amount: U256,
    pub expiration: U256,
    pub nonce: U256,
    pub fee_rate_bps: U256,
    pub side: u8,
    pub signature_type: u8,
}

#[derive(Debug, Clone, Copy)]
pub enum Side {
    Buy = 0,
    Sell = 1,
}

#[derive(Debug, Clone, Copy)]
pub enum SignatureType {
    Eoa = 0,
    PolyProxy = 1,
    PolyGnosisSafe = 2,
}

pub fn build_order(
    side: Side,
    price: f64,
    size_usdc: f64,
    token_id: U256,
    maker: Address,
    expiration: u64,
    nonce: u64,
    fee_rate_bps: u64,
    sig_type: SignatureType,
) -> anyhow::Result<PolymarketOrder> {
    // Pricing rules (Polymarket CTF outcome tokens are in 6 decimals, like USDC.e):
    //   For BUY at price p with $S notional:
    //     maker_amount = S          USDC.e   (we pay)
    //     taker_amount = S / p      outcome tokens (we receive)
    //   For SELL at price p with $S notional of position:
    //     maker_amount = S / p      outcome tokens (we deliver)
    //     taker_amount = S          USDC.e   (we receive)
    if price <= 0.0 || price >= 1.0 {
        anyhow::bail!("price must be in (0, 1), got {}", price);
    }
    if size_usdc <= 0.0 {
        anyhow::bail!("size_usdc must be positive, got {}", size_usdc);
    }

    let usdc_units: u128 = (size_usdc * 1_000_000.0) as u128;
    let token_units: u128 = ((size_usdc / price) * 1_000_000.0) as u128;

    let (maker_amount, taker_amount, side_u8) = match side {
        Side::Buy => (U256::from(usdc_units), U256::from(token_units), 0u8),
        Side::Sell => (U256::from(token_units), U256::from(usdc_units), 1u8),
    };

    let salt = U256::from(rand::random::<u128>());

    Ok(PolymarketOrder {
        salt,
        maker,
        signer: maker,
        taker: Address::zero(),
        token_id,
        maker_amount,
        taker_amount,
        expiration: U256::from(expiration),
        nonce: U256::from(nonce),
        fee_rate_bps: U256::from(fee_rate_bps),
        side: side_u8,
        signature_type: sig_type as u8,
    })
}
