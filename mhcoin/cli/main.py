"""mhcoin CLI — Milestone 2: wallet, chain, mining (local/regtest)."""

from __future__ import annotations

import getpass
import os
from pathlib import Path

import click

from mhcoin import __version__
from mhcoin.config_loader import resolve_data_dir, wallet_paths
from mhcoin.node.local_node import LocalNode
from mhcoin.wallet import Wallet, WalletError
from mhcoin.wallet.send import format_mhc


def _password(create: bool = False) -> str:
    """Interactive wallet password. Env MHCOIN_WALLET_PASSWORD skips prompts (tests/CI)."""
    env = os.environ.get("MHCOIN_WALLET_PASSWORD")
    if env:
        return env
    if create:
        pwd = getpass.getpass("Create wallet password: ")
        if not pwd:
            raise click.ClickException("password cannot be empty")
        confirm = getpass.getpass("Confirm password: ")
        if pwd != confirm:
            raise click.ClickException("passwords do not match")
        return pwd
    pwd = getpass.getpass("Wallet password: ")
    if not pwd:
        raise click.ClickException("password cannot be empty")
    return pwd


@click.group()
@click.version_option(__version__, prog_name="mhcoin")
def cli() -> None:
    """MHCOIN (MHC) — independent blockchain node & wallet."""


@cli.group()
def wallet() -> None:
    """Wallet operations."""


@wallet.command("create")
@click.option("--network", default=None, help="localnet | testnet | mainnet | regtest")
@click.option("--label", default="default", help="Wallet label")
@click.option("--password", default=None, help="Encryption password (or MHCOIN_WALLET_PASSWORD)")
@click.option(
    "--activate/--no-activate",
    default=None,
    help="Make this the active (default) wallet. Default: yes for first wallet, no when adding another.",
)
def wallet_create(
    network: str | None, label: str, password: str | None, activate: bool | None
) -> None:
    """Create an encrypted wallet (password required). Prints your MHC address."""
    paths = wallet_paths(network)
    pwd = password or os.environ.get("MHCOIN_WALLET_PASSWORD") or _password(create=True)
    w = Wallet(paths, password=pwd)
    existing = bool(w.list_wallets())
    make_default = (not existing) if activate is None else bool(activate)
    try:
        address = w.create(label=label, password=pwd, make_default=make_default)
    except WalletError as e:
        raise click.ClickException(str(e)) from e
    click.echo("Wallet created.")
    click.echo(f"Address: {address}")
    if label and label != "default":
        click.echo(f"Label: {label}")
    if make_default:
        click.echo("Active wallet: this address (default).")
    else:
        click.echo("Note: previous default wallet stays active (use --activate to switch).")


@wallet.command("address")
@click.option("--network", default=None)
@click.option("--label", default=None, help="Wallet label (default: primary wallet)")
def wallet_address(network: str | None, label: str | None) -> None:
    """Show MHC address (no password required)."""
    paths = wallet_paths(network)
    w = Wallet(paths)
    try:
        click.echo(w.address_for_label(label) if label else w.default_address())
    except WalletError as e:
        raise click.ClickException(str(e)) from e


@wallet.command("balance")
@click.option("--network", default=None)
@click.option("--address", default=None, help="Address to check (default: primary wallet)")
@click.option("--label", default=None, help="Wallet label to check")
def wallet_balance(network: str | None, address: str | None, label: str | None) -> None:
    """Show balance (no password required)."""
    paths = wallet_paths(network)
    w = Wallet(paths)
    try:
        if address:
            addr = address
        elif label:
            addr = w.address_for_label(label)
        else:
            addr = w.default_address()
        confirmed, unconfirmed = w.balance(addr)
    except WalletError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Address: {addr}")
    click.echo(f"Balance: {format_mhc(confirmed)} MHC")
    if unconfirmed:
        click.echo(f"Unconfirmed: {format_mhc(unconfirmed)} MHC")


@wallet.command("send")
@click.argument("address")
@click.argument("amount")
@click.option("--network", default=None)
@click.option("--password", default=None)
@click.option("--fee", default=None, help="Fee in MHC (default 0.00001000)")
@click.option("--data-dir", default=None, type=click.Path(), help="Wallet/chain data directory")
def wallet_send(
    address: str,
    amount: str,
    network: str | None,
    password: str | None,
    fee: str | None,
    data_dir: str | None,
) -> None:
    """Send MHC to an address. Asks for wallet password to sign.

    Example:  mhcoin wallet send mhc1petro... 10
    """
    paths = wallet_paths(network)
    if data_dir:
        from mhcoin.wallet.wallet import WalletPaths

        paths = WalletPaths(
            data_dir=Path(data_dir).expanduser().resolve(),
            network=paths.network,
            hrp=paths.hrp,
        )
    pwd = password or os.environ.get("MHCOIN_WALLET_PASSWORD") or _password()
    w = Wallet(paths, password=pwd)
    node = LocalNode(paths.data_dir, hrp=paths.hrp, network=paths.network)
    try:
        if node.chain.height < 0:
            raise click.ClickException(
                "no blockchain yet — mine first: mhcoin mining start --address <your-address>"
            )
        fee_sats = None
        if fee is not None:
            from mhcoin.wallet.send import parse_amount_mhc

            fee_sats = parse_amount_mhc(fee)
        result = w.send(address, amount, password=pwd, fee_sats=fee_sats, utxo=node.chain.utxo)
        node.submit_tx(result.tx)
        click.echo("Transaction sent to local mempool.")
        click.echo(f"TXID: {result.txid_hex}")
        click.echo(f"To: {address}")
        click.echo(f"Amount: {format_mhc(result.amount)} MHC")
        click.echo(f"Fee: {format_mhc(result.fee)} MHC")
        if result.change:
            click.echo(f"Change: {format_mhc(result.change)} MHC")
        click.echo("Next: a miner must include this TX in a block.")
        click.echo("  mhcoin mining start --address <your-address> --blocks 1")
    except WalletError as e:
        raise click.ClickException(str(e)) from e
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e
    finally:
        node.close()


@wallet.command("history")
@click.option("--network", default=None)
def wallet_history(network: str | None) -> None:
    click.echo("No history index yet (Milestone 3+).")


@cli.command("mempool")
@click.option("--network", default=None)
@click.option("--data-dir", default=None, type=click.Path())
@click.option("--info", "info_only", is_flag=True, help="Size only")
def mempool_cmd(network: str | None, data_dir: str | None, info_only: bool) -> None:
    """Show local mempool (size, TXID, fee, size)."""
    from mhcoin.transaction.transaction import fee_of
    from mhcoin.utxo import OutPoint

    dpath = Path(data_dir).expanduser().resolve() if data_dir else resolve_data_dir(network)
    paths = wallet_paths(network)
    node = LocalNode(dpath, hrp=paths.hrp, network=paths.network)
    try:
        mp = node.mempool
        click.echo(f"Mempool size: {len(mp)}")
        if info_only:
            return
        for tx in mp.list_txs():
            size = len(tx.serialize())
            fee = None
            try:
                vals = []
                for tin in tx.inputs:
                    e = node.chain.utxo.get(OutPoint(txid=tin.prev_txid, vout=tin.prev_vout))
                    if e is None:
                        vals = []
                        break
                    vals.append(e.output.value)
                if vals:
                    fee = fee_of(tx, vals)
            except Exception:
                fee = None
            fee_s = format_mhc(fee) if fee is not None else "?"
            click.echo(f"TXID: {tx.txid_hex()}")
            click.echo(f"  fee: {fee_s} MHC  size: {size} bytes")
    finally:
        node.close()


@cli.group("tx")
def tx_group() -> None:
    """Transaction lookup."""


@tx_group.command("get")
@click.argument("txid")
@click.option("--network", default=None)
@click.option("--data-dir", default=None, type=click.Path())
def tx_get(txid: str, network: str | None, data_dir: str | None) -> None:
    dpath = Path(data_dir).expanduser().resolve() if data_dir else resolve_data_dir(network)
    paths = wallet_paths(network)
    node = LocalNode(dpath, hrp=paths.hrp, network=paths.network)
    try:
        tx = node.mempool.get(txid.lower())
        if tx is None:
            raise click.ClickException("transaction not in mempool")
        click.echo(f"TXID: {tx.txid_hex()}")
        click.echo(f"Size: {len(tx.serialize())} bytes")
        click.echo(f"Inputs: {len(tx.inputs)}")
        click.echo(f"Outputs: {len(tx.outputs)}")
    finally:
        node.close()


@cli.group()
def blockchain() -> None:
    """Local chain operations."""


@blockchain.command("info")
@click.option("--network", default=None)
@click.option("--data-dir", default=None, type=click.Path())
def blockchain_info(network: str | None, data_dir: str | None) -> None:
    paths = wallet_paths(network)
    dpath = Path(data_dir).expanduser().resolve() if data_dir else paths.data_dir
    node = LocalNode(dpath, hrp=paths.hrp, network=paths.network)
    try:
        info = node.chain.info()
        click.echo(f"Height: {info['height']}")
        click.echo(f"Tip: {info['tip']}")
        click.echo(f"Work: {info['chain_work']}")
        click.echo(f"Known blocks: {info.get('known_blocks')}")
        click.echo(f"Side chains: {info.get('side_chains')}")
        click.echo(f"Orphans: {info.get('orphans')}")
        click.echo(f"UTXO count: {info['utxo_count']}")
        if info.get("utxo_fingerprint"):
            click.echo(f"UTXO fingerprint: {info['utxo_fingerprint']}")
        click.echo(f"Bits: 0x{info['bits']:08x}")
        click.echo(f"Issued supply: {info['issued_supply'] / 1e8:.8f} MHC")
        click.echo(f"Circulating (UTXO sum): {info['circulating_supply'] / 1e8:.8f} MHC")
        click.echo(f"Mempool: {len(node.mempool)}")
        try:
            node.chain.assert_supply_consistency()
            click.echo("Supply check: OK (circulating == issued)")
        except Exception as e:
            click.echo(f"Supply check: FAIL ({e})")
    finally:
        node.close()


@blockchain.command("forks")
@click.option("--network", default=None)
@click.option("--data-dir", default=None, type=click.Path())
def blockchain_forks(network: str | None, data_dir: str | None) -> None:
    """Show active tip and known side-chain tips (Stage 6)."""
    paths = wallet_paths(network)
    dpath = Path(data_dir).expanduser().resolve() if data_dir else paths.data_dir
    node = LocalNode(dpath, hrp=paths.hrp, network=paths.network)
    try:
        click.echo("Forks:")
        for branch in node.chain.forks_info():
            kind = "ACTIVE" if branch["active"] else "SIDE"
            click.echo(f"  * {kind}")
            click.echo(f"    tip: {branch['tip']}")
            click.echo(f"    height: {branch['height']}")
            click.echo(f"    work: {branch['work']}")
            if branch.get("ancestor"):
                click.echo(f"    ancestor: {branch['ancestor']}")
    finally:
        node.close()


@blockchain.command("height")
@click.option("--network", default=None)
def blockchain_height(network: str | None) -> None:
    paths = wallet_paths(network)
    node = LocalNode(paths.data_dir, hrp=paths.hrp, network=paths.network)
    try:
        click.echo(str(node.chain.height))
    finally:
        node.close()


@blockchain.command("init")
@click.option("--network", default=None)
@click.option("--password", default=None)
def blockchain_init(network: str | None, password: str | None) -> None:
    """Mine custom regtest/localnet genesis paying the default wallet (local only).

    Mainnet/testnet forbid custom genesis — use frozen genesis via `node start`.
    """
    from mhcoin.consensus.params import get_network_params

    paths = wallet_paths(network)
    params = get_network_params(paths.network)
    if not params.allow_custom_genesis_bootstrap:
        raise click.ClickException(
            f"{params.name} forbids custom genesis bootstrap. "
            "Frozen genesis is installed automatically on first `mhcoin node start`."
        )
    pwd = password or os.environ.get("MHCOIN_WALLET_PASSWORD") or _password()
    w = Wallet(paths, password=pwd)
    try:
        addr = w.default_address()
    except WalletError:
        addr = w.create(password=pwd)
    node = LocalNode(paths.data_dir, hrp=paths.hrp, network=paths.network)
    try:
        gh = node.bootstrap_genesis(addr)
        click.echo(f"{params.name} custom genesis initialized")
        click.echo(f"Genesis hash: {gh}")
        click.echo(f"Miner address: {addr}")
        click.echo(f"Height: {node.chain.height}")
    except Exception as e:
        raise click.ClickException(str(e)) from e
    finally:
        node.close()


@cli.group()
def genesis() -> None:
    """Frozen genesis show / verify (Stage 9). Never auto-launches mainnet."""


@genesis.command("show")
@click.option("--network", required=True, type=click.Choice(["mainnet", "testnet", "regtest", "localnet"]))
def genesis_show(network: str) -> None:
    """Print frozen genesis parameters for a network."""
    import json

    from mhcoin.blockchain.genesis import verify_frozen_genesis

    click.echo(json.dumps(verify_frozen_genesis(network), indent=2))


@genesis.command("verify")
@click.option("--network", required=True, type=click.Choice(["mainnet", "testnet", "regtest", "localnet"]))
def genesis_verify(network: str) -> None:
    """Rebuild template + PoW and compare to frozen constants."""
    from mhcoin.blockchain.genesis import verify_frozen_genesis

    info = verify_frozen_genesis(network)
    click.echo(f"OK  {network}")
    click.echo(f"genesis_hash: {info['genesis_hash']}")
    click.echo(f"merkle_root:  {info['merkle_root']}")
    click.echo(f"nonce: {info['nonce']}  timestamp: {info['timestamp']}")
    click.echo(f"bits: {info['bits_hex']}  magic: {info['magic']}  port: {info['default_port']}")


@genesis.command("verify-all")
def genesis_verify_all() -> None:
    from mhcoin.blockchain.genesis import verify_frozen_genesis
    from mhcoin.consensus.params import list_networks

    for name in list_networks():
        verify_frozen_genesis(name)
        click.echo(f"OK  {name}")


@cli.group("block")
def block_group() -> None:
    """Block inspection."""


@block_group.command("get")
@click.argument("block_hash")
@click.option("--network", default=None)
@click.option("--data-dir", default=None, type=click.Path())
def block_get(block_hash: str, network: str | None, data_dir: str | None) -> None:
    paths = wallet_paths(network)
    dpath = Path(data_dir).expanduser().resolve() if data_dir else paths.data_dir
    node = LocalNode(dpath, hrp=paths.hrp, network=paths.network)
    try:
        try:
            bh = bytes.fromhex(block_hash)
        except ValueError as e:
            raise click.ClickException("invalid block hash hex") from e
        block = node.chain.get_block_by_hash(bh)
        if block is None:
            raise click.ClickException("block not found")
        click.echo(f"Hash: {block.block_hash().hex()}")
        click.echo(f"Prev: {block.header.previous_block_hash.hex()}")
        click.echo(f"Merkle: {block.header.merkle_root.hex()}")
        click.echo(f"Timestamp: {block.header.timestamp}")
        click.echo(f"Bits: 0x{block.header.bits:08x}")
        click.echo(f"Nonce: {block.header.nonce}")
        click.echo(f"Tx count: {len(block.transactions)}")
        click.echo(f"Size: {len(block.serialize())} bytes")
    finally:
        node.close()


@cli.group()
def mining() -> None:
    """Mining (solo PoW)."""


@mining.command("mine")
@click.option("--network", default=None)
@click.option("--password", default=None)
@click.option("--data-dir", default=None, type=click.Path())
@click.option("--address", default=None, help="Reward address (default: wallet default)")
def mining_mine(
    network: str | None,
    password: str | None,
    data_dir: str | None,
    address: str | None,
) -> None:
    """Mine exactly one block (reward → --address or default wallet)."""
    from mhcoin.mining.miner import SoloMiner
    from mhcoin.wallet.addresses import validate_address

    paths = wallet_paths(network)
    if data_dir:
        from mhcoin.wallet.wallet import WalletPaths

        paths = WalletPaths(
            data_dir=Path(data_dir).expanduser().resolve(),
            network=paths.network,
            hrp=paths.hrp,
        )
    if address:
        if not validate_address(address, hrp=paths.hrp):
            raise click.ClickException(f"invalid address for network {paths.network}")
        addr = address
    else:
        pwd = password or os.environ.get("MHCOIN_WALLET_PASSWORD") or _password()
        w = Wallet(paths, password=pwd)
        try:
            addr = w.default_address()
        except WalletError as e:
            raise click.ClickException(str(e)) from e
    miner = SoloMiner(
        data_dir=paths.data_dir,
        network=paths.network,
        hrp=paths.hrp,
        address=addr,
    )
    try:
        miner.ensure_chain()
        result = miner.mine_one()
        click.echo(f"Mined block height={result.height}")
        click.echo(f"Block hash: {result.block_hash}")
        click.echo(f"Reward: {format_mhc(result.reward_sats)} MHC → {result.address}")
    except Exception as e:
        raise click.ClickException(str(e)) from e
    finally:
        miner.close()


@mining.command("start")
@click.option("--address", required=True, help="Reward MHC address (coinbase payout)")
@click.option("--network", default=None, help="localnet | testnet | mainnet | regtest")
@click.option("--data-dir", default=None, type=click.Path(), help="Chain/wallet data directory")
@click.option("--blocks", default=None, type=int, help="Stop after N found blocks (default: run until Ctrl+C)")
@click.option(
    "--workers",
    default=None,
    type=int,
    help="PoW CPU processes (default: all cores; or MHCOIN_MINER_WORKERS)",
)
def mining_start(
    address: str,
    network: str | None,
    data_dir: str | None,
    blocks: int | None,
    workers: int | None,
) -> None:
    """Continuous solo mining to --address. No extra config needed.

    Example:
      mhcoin mining start --address mhc1...
    """
    from mhcoin.mining.miner import SoloMiner
    from mhcoin.wallet.addresses import validate_address

    paths = wallet_paths(network)
    if data_dir:
        from mhcoin.wallet.wallet import WalletPaths

        paths = WalletPaths(
            data_dir=Path(data_dir).expanduser().resolve(),
            network=paths.network,
            hrp=paths.hrp,
        )
    if not validate_address(address, hrp=paths.hrp):
        raise click.ClickException(
            f"invalid address for network={paths.network} (expected hrp={paths.hrp})"
        )
    miner = SoloMiner(
        data_dir=paths.data_dir,
        network=paths.network,
        hrp=paths.hrp,
        address=address,
        workers=workers,
    )
    results = miner.run(max_blocks=blocks)
    if results:
        click.echo(f"\nMined {len(results)} block(s). Check: mhcoin wallet balance")


@cli.group()
def node() -> None:
    """P2P node (Stage 2: handshake / ping)."""


@node.command("start")
@click.option("--network", default="localnet", show_default=True)
@click.option("--host", default="0.0.0.0", show_default=True, help="Listen bind address")
@click.option("--port", default=None, type=int, help="Listen port (default: network default)")
@click.option("--max-peers", default=32, type=int)
@click.option("--data-dir", default=None, type=click.Path())
@click.option(
    "--connect",
    multiple=True,
    help="host:port to dial (repeatable). If omitted on mainnet, uses built-in seeds.",
)
@click.option("--no-seed", is_flag=True, help="Do not auto-dial default seeds when --connect is empty")
def node_start(
    network: str,
    host: str,
    port: int | None,
    max_peers: int,
    data_dir: str | None,
    connect: tuple[str, ...],
    no_seed: bool,
) -> None:
    """Start P2P listener (foreground). Use a separate --data-dir per node.

    Does not invent genesis: installs the frozen genesis for --network on first start.
    Default network is localnet (never auto-selects mainnet).

    Bootstrap is Bitcoin-style: optional seeds for first contact, then ADDR gossip.
    """
    import logging

    from mhcoin.consensus.params import PROTOCOL_VERSION, get_network_params
    from mhcoin.network.seeds import default_connect_peers

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    from mhcoin.node.runtime import NodeRuntime

    params = get_network_params(network)
    listen_port = port if port is not None else params.default_port
    if data_dir:
        dpath = Path(data_dir).expanduser().resolve()
    else:
        dpath = resolve_data_dir(params.name) / f"node-{listen_port}"

    dial = list(connect)
    if not dial and not no_seed:
        dial = default_connect_peers(params.name)

    rt = NodeRuntime(
        data_dir=dpath,
        network=params.name,
        host=host,
        port=listen_port,
        max_peers=max_peers,
        connect=dial,
        enable_listen=True,
    )
    click.echo(f"Starting MHCOIN node on {host}:{listen_port} ({params.name})")
    click.echo(f"Data dir: {dpath}")
    click.echo(f"Genesis: {params.genesis_hash_hex}")
    click.echo(f"Magic: {params.magic.hex()}  protocol={PROTOCOL_VERSION}")
    rt.start(blocking=True)


@node.command("info")
@click.option("--data-dir", required=True, type=click.Path(exists=True))
def node_info(data_dir: str) -> None:
    from mhcoin.node.runtime import NodeRuntime

    status = NodeRuntime.read_status(Path(data_dir))
    if not status:
        raise click.ClickException("node status not found — is the node running?")
    click.echo(f"Network: {status['network']}")
    click.echo(f"Listen: {status['listen']}")
    click.echo(f"Protocol: {status['protocol']}")
    if status.get("software_version"):
        click.echo(f"Software: {status['software_version']}")
    if status.get("magic"):
        click.echo(f"Magic: {status['magic']}")
    if status.get("genesis_hash"):
        click.echo(f"Genesis: {status['genesis_hash']}")
    click.echo(f"Peers: {status['peer_count']}")
    click.echo(f"Height: {status['height']}")
    if status.get("tip"):
        click.echo(f"Tip: {status['tip']}")
    if "chain_work" in status:
        click.echo(f"Work: {status['chain_work']}")
    if status.get("known_blocks") is not None:
        click.echo(f"Known blocks: {status['known_blocks']}")
    if status.get("side_chains") is not None:
        click.echo(f"Side chains: {status['side_chains']}")
    if status.get("utxo_fingerprint"):
        click.echo(f"UTXO fingerprint: {status['utxo_fingerprint']}")
    if "mempool_size" in status:
        click.echo(f"Mempool: {status['mempool_size']}")
    if status.get("known_addrs") is not None:
        click.echo(f"Known addrs: {status['known_addrs']}")
    if status.get("bans") is not None:
        click.echo(f"Bans: {status['bans']}")
    sync = status.get("sync") or {}
    if sync:
        click.echo(
            f"Sync: {sync.get('state')} peer={sync.get('peer')} "
            f"progress={sync.get('progress_height')}/{sync.get('target_hint')}"
        )
    click.echo(f"PID: {status.get('pid')}")


@node.command("addrs")
@click.option("--data-dir", required=True, type=click.Path(exists=True))
def node_addrs(data_dir: str) -> None:
    """Show known peer addresses from AddrDB (Stage 7)."""
    from mhcoin.network.addrdb import AddrDB

    db = AddrDB(Path(data_dir) / "peers.sqlite")
    try:
        rows = db.list_recent(limit=100)
        click.echo(f"Known addresses: {db.count()}")
        for r in rows:
            click.echo(
                f"  {r.host}:{r.port}  seen={r.last_seen}  "
                f"attempts={r.attempts}  source={r.source}"
            )
    finally:
        db.close()


@node.command("bans")
@click.option("--data-dir", required=True, type=click.Path(exists=True))
def node_bans(data_dir: str) -> None:
    """Show banned hosts (Stage 7)."""
    from mhcoin.network.ban import BanManager

    bans = BanManager(Path(data_dir) / "bans.json")
    rows = bans.list_bans()
    if not rows:
        click.echo("No bans.")
        return
    for b in rows:
        click.echo(f"  {b.host}  until={int(b.until)}  reason={b.reason}  score={b.score}")


@node.command("peers")
@click.option("--data-dir", required=True, type=click.Path(exists=True))
def node_peers(data_dir: str) -> None:
    from mhcoin.node.runtime import NodeRuntime

    status = NodeRuntime.read_status(Path(data_dir))
    if not status:
        raise click.ClickException("node status not found — is the node running?")
    peers = status.get("peers") or []
    if not peers:
        click.echo("No peers.")
        return
    for p in peers:
        click.echo(
            f"{p['addr']}  dir={'inbound' if p['inbound'] else 'outbound'}  "
            f"state={p['state']}  protocol={p.get('protocol')}  height={p.get('height')}"
        )


@node.command("stop")
@click.option("--data-dir", required=True, type=click.Path(exists=True))
def node_stop(data_dir: str) -> None:
    import os
    import signal

    pid_path = Path(data_dir) / "node.pid"
    if not pid_path.is_file():
        raise click.ClickException("node.pid not found")
    pid = int(pid_path.read_text().strip())
    os.kill(pid, signal.SIGTERM)
    click.echo(f"Sent SIGTERM to {pid}")


@cli.group()
def audit() -> None:
    """RC1 pre-launch audit (does NOT launch mainnet)."""


@audit.command("rc1")
def audit_rc1() -> None:
    """Run automated Release Candidate 1 checklist."""
    import json

    from mhcoin.audit.rc1 import format_audit_report, run_rc1_audit

    report = run_rc1_audit()
    click.echo(format_audit_report(report))
    click.echo(json.dumps({"ok": report["ok"], "fingerprint": report["consensus_fingerprint"]}, indent=2))
    if not report["ok"]:
        raise SystemExit(1)


@audit.command("genesis")
@click.option("--network", default="mainnet", show_default=True)
def audit_genesis(network: str) -> None:
    """Independent genesis recompute (header → HASH256 → PoW)."""
    import json

    from mhcoin.audit.rc1 import independent_recompute_genesis

    click.echo(json.dumps(independent_recompute_genesis(network), indent=2))


@audit.command("identity")
def audit_identity() -> None:
    """Print consensus identity JSON for cross-machine comparison."""
    import json

    from mhcoin.audit.rc1 import consensus_identity

    click.echo(json.dumps(consensus_identity(), indent=2, sort_keys=True))


@audit.command("fingerprint")
def audit_fingerprint() -> None:
    """Print consensus source tree fingerprint."""
    import json

    from mhcoin.audit.rc1 import consensus_source_fingerprint

    click.echo(json.dumps(consensus_source_fingerprint(), indent=2))


if __name__ == "__main__":
    import multiprocessing as mp

    mp.freeze_support()
    cli(prog_name="mhcoin")
