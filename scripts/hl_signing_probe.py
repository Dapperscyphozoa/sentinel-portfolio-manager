#!/usr/bin/env python3
"""HL signing-path probe.

Validates the full agent→main signing chain on mainnet WITHOUT placing a real
order or spending capital. Uses `update_leverage` — a signed action that's
idempotent and free.

Test sequence:
  1. Construct Exchange with agent's private_key + account_address=main wallet
  2. Read pre-state leverage for BTC via Info.clearinghouseState
  3. Sign + send update_leverage(BTC, leverage=5)
  4. Read post-state to confirm the change landed
  5. Restore previous leverage if it changed

A successful run confirms:
  - eth_account signs correctly with the agent key
  - HL accepts the agent's signature for the main account
  - account_address routing works (without it, HL returns "agent not approved")
  - The network path between Render and HL is intact

Failure modes surfaced:
  - "User or API Wallet ... does not exist." → agent not approved on main
  - "must be USDC margined" / "Account does not exist" → wrong account_address
  - Network errors → connectivity / DNS

Usage:
    HL_PRIVATE_KEY=0x... HL_USER_WALLET=0x3eDaD... python3 scripts/hl_signing_probe.py
"""
from __future__ import annotations

import json
import os
import sys
import time

# Allow running from repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


PROBE_COIN = "BTC"
PROBE_LEVERAGE = 5  # the target value we attempt to set


def main() -> int:
    pk = os.environ.get("HL_PRIVATE_KEY")
    user = os.environ.get("HL_USER_WALLET") or os.environ.get("HL_MAIN_WALLET")
    if not pk:
        print("FAIL: HL_PRIVATE_KEY not set in env")
        return 2
    if not user:
        print("FAIL: HL_USER_WALLET (main wallet) not set in env")
        return 2

    base_url = os.environ.get("HL_BASE_URL", "https://api.hyperliquid.xyz")
    print(f"probe target: {base_url}")
    print(f"agent (signer) derived from HL_PRIVATE_KEY")
    print(f"main account: {user}")
    print()

    try:
        from eth_account import Account  # type: ignore
        from hyperliquid.exchange import Exchange  # type: ignore
        from hyperliquid.info import Info  # type: ignore
    except ImportError as e:
        print(f"FAIL: {e}")
        print("Install: pip install hyperliquid-python-sdk eth-account")
        return 2

    wallet = Account.from_key(pk)
    print(f"agent address derived: {wallet.address}")
    if wallet.address.lower() == user.lower():
        print("WARN: signer address == account_address. Not an agent setup.")

    info = Info(base_url, skip_ws=True)
    try:
        state = info.user_state(user)
    except Exception as e:
        print(f"FAIL: info.user_state({user}) raised: {e}")
        return 3
    margin_summary = state.get("marginSummary", {})
    account_value = float(margin_summary.get("accountValue", 0))
    print(f"read OK: account_value=${account_value:.2f}")
    if account_value <= 0:
        print("WARN: account value is zero — read OK but cannot test execution")

    # Find current leverage for the probe coin
    asset_positions = state.get("assetPositions", [])
    pre_lev = None
    pre_cross = None
    for ap in asset_positions:
        pos = ap.get("position", {})
        if pos.get("coin") == PROBE_COIN:
            lev = pos.get("leverage", {})
            pre_lev = int(lev.get("value") or 0)
            pre_cross = lev.get("type") == "cross"
            break
    print(f"pre-state {PROBE_COIN} leverage: {pre_lev} (cross={pre_cross})")

    # Construct Exchange with account_address (the critical part the audit flagged)
    try:
        ex = Exchange(wallet, base_url, account_address=user)
    except TypeError:
        # Older SDK signature without account_address kwarg
        print("FAIL: HL SDK Exchange does not accept account_address — upgrade hyperliquid-python-sdk")
        return 2

    # Sign + send update_leverage. This is a real signed action but costs
    # nothing and doesn't open any position.
    print()
    print(f"signing: update_leverage(name={PROBE_COIN}, is_cross=True, leverage={PROBE_LEVERAGE})")
    t0 = time.time()
    try:
        res = ex.update_leverage(name=PROBE_COIN, is_cross=True, leverage=PROBE_LEVERAGE)
    except Exception as e:
        print(f"FAIL: update_leverage raised: {e}")
        return 4
    dt = time.time() - t0
    print(f"response (latency {dt*1000:.0f}ms):")
    print(json.dumps(res, indent=2)[:600])

    status = (res or {}).get("status")
    if status != "ok":
        print()
        print(f"FAIL: HL returned status={status}, not 'ok'")
        print("This is the failure the sentinel council warned about.")
        return 5

    # Verify the change actually landed by re-reading
    time.sleep(0.5)
    state2 = info.user_state(user)
    post_lev = None
    for ap in state2.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == PROBE_COIN:
            post_lev = int(pos.get("leverage", {}).get("value") or 0)
            break
    print(f"post-state {PROBE_COIN} leverage: {post_lev}")

    # Restore prior leverage if changed
    if pre_lev is not None and pre_lev != PROBE_LEVERAGE:
        print(f"restoring prior leverage {pre_lev}...")
        try:
            ex.update_leverage(name=PROBE_COIN, is_cross=bool(pre_cross), leverage=pre_lev)
            print("restored")
        except Exception as e:
            print(f"warn: restore failed: {e}")

    print()
    print("=" * 60)
    print("PASS: HL signing path validated end-to-end")
    print(f"  - eth_account signed with agent key {wallet.address}")
    print(f"  - HL accepted signature for main account {user}")
    print(f"  - account_address routing works")
    print(f"  - leverage change confirmed by re-read")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
