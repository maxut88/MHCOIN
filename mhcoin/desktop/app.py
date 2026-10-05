"""MHCOIN Core Desktop.

Apple system Tk (8.5) breaks pack()/pady layouts — buttons stack on top of each other.
ALL interactive controls use place() with explicit Y coordinates. No pack for buttons.
Welcome is destroyed before main. Only one tab body exists at a time.
"""

from __future__ import annotations

import os
import subprocess
import sys

try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog
except ModuleNotFoundError as e:  # pragma: no cover
    raise SystemExit(f"MHCOIN Core Desktop needs Tk.\nDetail: {e}") from e

from mhcoin.desktop.controller import CoreController
from mhcoin.wallet.send import format_mhc
from mhcoin.wallet.wallet import WalletError

APP_TITLE = "MHCOIN"
BG = "#f0f2f5"
CARD = "#ffffff"
ACCENT = "#2d8a5e"
TEXT = "#111111"
MUTED = "#555555"
BTN_H = 40
BTN_W = 280
GAP = 18
LEFT = 24


def _mac() -> bool:
    return sys.platform == "darwin"


def _font(size: int = 12, bold: bool = False):
    fam = "Helvetica" if _mac() else "Segoe UI"
    return (fam, size, "bold") if bold else (fam, size)


class VPlace:
    """Strict top-to-bottom placer — impossible for widgets to share the same Y."""

    def __init__(self, parent: tk.Misc, x: int = LEFT, y: int = 16, gap: int = GAP):
        self.parent = parent
        self.x = x
        self.y = y
        self.gap = gap

    def skip(self, px: int) -> None:
        self.y += px

    def label(self, text: str, *, muted: bool = False, title: bool = False, big: bool = False) -> tk.Label:
        size, bold = 12, False
        if big:
            size, bold = 24, True
        elif title:
            size, bold = 16, True
        elif muted:
            size = 11
        h = size + 14
        lbl = tk.Label(
            self.parent,
            text=text,
            font=_font(size, bold),
            fg=MUTED if muted else TEXT,
            bg=self.parent.cget("bg") if self.parent.cget("bg") else BG,
            anchor="w",
            justify="left",
        )
        lbl.place(x=self.x, y=self.y, width=520, height=h)
        self.y += h + (8 if not big else 12)
        return lbl

    def button(self, text: str, command, *, accent: bool = False, width: int = BTN_W) -> tk.Button:
        b = tk.Button(
            self.parent,
            text=text,
            command=command,
            font=_font(12, accent),
            bg=ACCENT if accent else "#e0e0e0",
            fg="#ffffff" if accent else TEXT,
            activebackground=ACCENT if accent else "#d0d0d0",
            activeforeground="#ffffff" if accent else TEXT,
            relief="raised",
            borderwidth=2,
            highlightthickness=0,
            cursor="hand2",
        )
        b.place(x=self.x, y=self.y, width=width, height=BTN_H)
        self.y += BTN_H + self.gap
        return b

    def entry(self, width: int = 520) -> tk.Entry:
        e = tk.Entry(
            self.parent,
            font=_font(13),
            bg="#ffffff",
            fg=TEXT,
            insertbackground=TEXT,
            relief="solid",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#999999",
            highlightcolor=ACCENT,
        )
        e.place(x=self.x, y=self.y, width=width, height=34)
        self.y += 34 + 14
        return e

    def text(self, height_px: int = 90, width: int = 520) -> tk.Text:
        t = tk.Text(
            self.parent,
            wrap="word",
            font=("Menlo", 13) if _mac() else ("Consolas", 12),
            bg="#ffffff",
            fg=TEXT,
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
        )
        t.place(x=self.x, y=self.y, width=width, height=height_px)
        self.y += height_px + 18
        return t

    def listbox(self, height_px: int = 180, width: int = 520) -> tk.Listbox:
        lb = tk.Listbox(
            self.parent,
            font=_font(11),
            bg="#ffffff",
            fg=TEXT,
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            selectbackground=ACCENT,
        )
        lb.place(x=self.x, y=self.y, width=width, height=height_px)
        self.y += height_px + 12
        return lb


class MhcoinDesktop(tk.Tk):
    TABS = [
        ("Overview", "Overview"),
        ("Receive", "Receive"),
        ("Send", "Send"),
        ("Mining", "Mining"),
        ("Txs", "Txs"),
        ("Network", "Network"),
        ("Settings", "Settings"),
    ]

    def __init__(self, network: str = "localnet"):
        super().__init__()
        self.title(f"{APP_TITLE} Core")
        self.geometry("720x760")
        self.minsize(680, 700)
        self.configure(bg=BG)
        self._set_app_icon()
        self.ctrl = CoreController(network=network)
        self._screen = "welcome"
        self._active_tab = "Overview"
        self._tab_btns: dict[str, tk.Button] = {}
        self._tick_on = False
        self._panel: tk.Frame | None = None

        self.container = tk.Frame(self, bg=BG)
        self.container.pack(fill="both", expand=True)
        self._show_welcome()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_app_icon(self) -> None:
        """Use MHCOIN logo instead of the default Python/Tk glyph."""
        try:
            from pathlib import Path

            assets = Path(__file__).resolve().parent / "assets"
            for name in ("mhcoin.png", "mhcoin-256.png", "mhcoin-64.png"):
                p = assets / name
                if not p.is_file():
                    continue
                try:
                    img = tk.PhotoImage(file=str(p))
                    self.iconphoto(True, img)
                    self._app_icon = img  # keep ref
                    return
                except tk.TclError:
                    continue
            ico = assets / "mhcoin.ico"
            if ico.is_file():
                try:
                    self.iconbitmap(default=str(ico))
                except tk.TclError:
                    pass
        except Exception:
            pass

    def _wipe(self) -> None:
        for w in list(self.container.winfo_children()):
            w.destroy()
        self._panel = None
        self._tab_btns.clear()
        self.update_idletasks()

    def _alive(self, w) -> bool:
        try:
            return w is not None and bool(w.winfo_exists())
        except tk.TclError:
            return False

    # --- welcome ------------------------------------------------------------

    def _show_welcome(self) -> None:
        self._tick_on = False
        self._screen = "welcome"
        self._wipe()
        panel = tk.Frame(self.container, bg=BG)
        panel.place(x=0, y=0, relwidth=1, relheight=1)
        self._panel = panel

        v = VPlace(panel, x=200, y=120, gap=22)
        title = tk.Label(panel, text="MHCOIN", font=_font(28, True), fg=TEXT, bg=BG)
        title.place(x=200, y=80, width=320, height=40)
        v.y = 140
        v.label("Welcome to MHCOIN", muted=True)
        v.skip(10)
        v.button("Create New Wallet", self._create_wallet, accent=True, width=300)
        v.button("Restore from Seed", self._restore_wallet, width=300)
        v.button("Open Existing Wallet", self._open_existing, width=300)
        v.skip(20)
        v.label("localnet RC only — not mainnet", muted=True)

    # --- password (native on macOS) -----------------------------------------

    def _macos_prompt(self, title: str, prompt: str, *, hidden: bool = True) -> str | None:
        def esc(s: str) -> str:
            return s.replace("\\", "\\\\").replace('"', '\\"')

        hide = " with hidden answer" if hidden else ""
        script = (
            f'set theResult to display dialog "{esc(prompt)}" with title "{esc(title)}" '
            f'default answer "" buttons {{"Cancel", "OK"}} default button "OK" '
            f'cancel button "Cancel"{hide}\n'
            f'return text returned of theResult'
        )
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        return (r.stdout or "").rstrip("\n") or None

    def _ask_password(self, prompt: str) -> str | None:
        if _mac():
            return self._macos_prompt(APP_TITLE, prompt, hidden=True)
        return simpledialog.askstring(APP_TITLE, prompt, show="*", parent=self)

    def _ask_new_password(self) -> str | None:
        if _mac():
            a = self._macos_prompt(APP_TITLE, "Choose a wallet password:", hidden=True)
            if not a:
                return None
            b = self._macos_prompt(APP_TITLE, "Confirm password:", hidden=True)
            if not b:
                return None
            if a != b:
                messagebox.showerror(APP_TITLE, "Passwords do not match")
                return None
            return a
        a = simpledialog.askstring(APP_TITLE, "Choose a wallet password:", show="*", parent=self)
        if not a:
            return None
        b = simpledialog.askstring(APP_TITLE, "Confirm password:", show="*", parent=self)
        if not b or a != b:
            if b is not None and a != b:
                messagebox.showerror(APP_TITLE, "Passwords do not match")
            return None
        return a

    def _create_wallet(self) -> None:
        pwd = self._ask_new_password()
        if not pwd:
            return
        try:
            created = self.ctrl.create_hd_wallet(pwd)
        except WalletError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        seed = created.get("mnemonic") or ""
        msg = (
            f"Your MHCOIN address:\n\n{created['address']}\n\n"
            f"Write down this recovery seed (BIP39):\n\n{seed}\n\n"
            f"Anyone with these words can spend your MHC.\n"
            f"Also back up your wallet password."
        )
        messagebox.showinfo(APP_TITLE, msg)
        self._enter_main()

    def _restore_wallet(self) -> None:
        words = simpledialog.askstring(
            APP_TITLE,
            "Enter BIP39 recovery seed (12 or 24 words):",
            parent=self,
        )
        if not words or not words.strip():
            return
        pwd = self._ask_new_password()
        if not pwd:
            return
        try:
            addr = self.ctrl.restore_wallet(pwd, words)
        except WalletError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        messagebox.showinfo(
            APP_TITLE,
            f"Wallet restored.\n\nAddress:\n{addr}\n\nBack up your password.",
        )
        self._enter_main()

    def _open_existing(self) -> None:
        if not self.ctrl.wallet_exists():
            messagebox.showwarning(APP_TITLE, "No wallet found. Create a new wallet first.")
            return
        pwd = self._ask_password("Wallet password:")
        if not pwd:
            return
        try:
            self.ctrl.unlock(pwd)
            _ = self.ctrl.default_address()
        except WalletError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.ctrl.ensure_chain()
        self._enter_main()

    # --- main ---------------------------------------------------------------

    def _enter_main(self) -> None:
        self._screen = "main"
        self._wipe()  # welcome gone forever
        panel = tk.Frame(self.container, bg=BG)
        panel.place(x=0, y=0, relwidth=1, relheight=1)
        self._panel = panel

        # Header
        tk.Label(panel, text="MHCOIN", font=_font(18, True), fg=TEXT, bg=BG).place(
            x=16, y=10, width=160, height=28
        )
        self.status_lbl = tk.Label(panel, text="", font=_font(11), fg=MUTED, bg=BG, anchor="e")
        self.status_lbl.place(x=200, y=12, width=500, height=24)

        # Tab buttons — fixed X slots so they never overlap
        self._tab_btns.clear()
        tab_w, tab_h, gap = 88, 32, 6
        x0, y0 = 12, 48
        for i, (key, label) in enumerate(self.TABS):
            x = x0 + i * (tab_w + gap)
            b = tk.Button(
                panel,
                text=label,
                font=_font(10),
                command=lambda n=key: self._select_tab(n),
                bg="#dddddd",
                fg=TEXT,
                relief="raised",
                borderwidth=1,
                highlightthickness=0,
            )
            b.place(x=x, y=y0, width=tab_w, height=tab_h)
            self._tab_btns[key] = b

        # Content body below tabs (absolute band — no pack overlap)
        self._body = tk.Frame(panel, bg=BG, highlightthickness=0)
        self._body.place(x=8, y=88, relwidth=1, width=-16, relheight=1, height=-96)
        self.update_idletasks()

        self._select_tab("Overview")
        self._tick_on = True
        self.after(400, self._tick)

    def _select_tab(self, name: str) -> None:
        if self._screen != "main":
            return
        self._active_tab = name
        for w in list(self._body.winfo_children()):
            w.destroy()
        self.update_idletasks()

        for n, btn in self._tab_btns.items():
            if n == name:
                btn.configure(bg=ACCENT, fg="#ffffff", relief="sunken")
            else:
                btn.configure(bg="#dddddd", fg=TEXT, relief="raised")

        {
            "Overview": self._build_overview,
            "Receive": self._build_receive,
            "Send": self._build_send,
            "Mining": self._build_mining,
            "Txs": self._build_txs,
            "Network": self._build_network,
            "Settings": self._build_settings,
        }[name]()
        self.refresh_all()

    def _page(self) -> tuple[tk.Frame, VPlace]:
        """White card page filling body; returns (card, placer)."""
        card = tk.Frame(self._body, bg=CARD, highlightthickness=1, highlightbackground="#cccccc")
        card.place(x=12, y=8, relwidth=1, relheight=1, width=-24, height=-16)
        return card, VPlace(card, x=20, y=18, gap=GAP)

    # --- pages --------------------------------------------------------------

    def _build_overview(self) -> None:
        card, v = self._page()
        v.label("Balance", muted=True)
        self.balance_lbl = v.label("0.00000000 MHC", big=True)
        v.skip(6)
        v.button("Receive", lambda: self._select_tab("Receive"))
        v.button("Send", lambda: self._select_tab("Send"))
        v.button("Mining", lambda: self._select_tab("Mining"), accent=True)
        v.skip(8)
        v.label("Recent activity", muted=True)
        self.overview_list = v.listbox(height_px=160)
        self.overview_sync = v.label("", muted=True)

    def _build_receive(self) -> None:
        card, v = self._page()
        v.label("Receive MHCOIN", title=True)
        v.label("Share this address to receive MHC.", muted=True)
        v.skip(8)
        self.recv_addr = v.text(height_px=100)
        v.skip(8)
        v.button("Copy Address", self._copy_address, accent=True)

    def _build_send(self) -> None:
        card, v = self._page()
        v.label("Send MHCOIN", title=True)
        v.label("Address", muted=True)
        self.send_addr = v.entry()
        v.label("Amount (MHC)", muted=True)
        self.send_amount = v.entry(width=240)
        v.label("Fee (MHC, optional)", muted=True)
        self.send_fee = v.entry(width=240)
        self.send_fee.insert(0, "0.00001000")
        v.skip(6)
        v.button("Send", self._do_send, accent=True)

    def _build_txs(self) -> None:
        card, v = self._page()
        v.label("Transactions", title=True)
        self.tx_list = v.listbox(height_px=420)

    def _build_mining(self) -> None:
        card, v = self._page()
        v.label("Mining", title=True)
        v.label("Reward address", muted=True)
        self.mine_addr = v.entry()
        self.mine_status = v.label("Status: Stopped")
        self.mine_hash = v.label("Hashrate: -", muted=True)
        self.mine_blocks = v.label("Blocks found: 0", muted=True)
        self.mine_rewards = v.label("Rewards: 0 MHC", muted=True)
        v.skip(8)
        v.button("START MINING", self._start_mine, accent=True)
        v.button("STOP MINING", self._stop_mine)

    def _build_network(self) -> None:
        card, v = self._page()
        v.label("Network", title=True)
        self.net_labels = {
            "network": v.label("Network: -"),
            "sync": v.label("Status: -"),
            "height": v.label("Block height: -"),
            "peers": v.label("Peers: -"),
            "tip": v.label("Tip: -"),
        }
        v.skip(8)
        v.button("Start Node", self._start_node)
        v.button("Stop Node", self._stop_node)
        v.label("Same data directory as the wallet.", muted=True)

    def _build_settings(self) -> None:
        card, v = self._page()
        v.label("Settings", title=True)
        self.settings_net = v.label("")
        self.settings_dir = v.label("", muted=True)
        v.skip(8)
        v.label("Network via MHCOIN_NETWORK (localnet for RC).", muted=True)
        v.skip(8)
        v.button("Refresh", self.refresh_all)

    # --- actions ------------------------------------------------------------

    def _copy_address(self) -> None:
        try:
            addr = self.ctrl.default_address()
        except WalletError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.clipboard_clear()
        self.clipboard_append(addr)
        messagebox.showinfo(APP_TITLE, f"Address copied:\n\n{addr}")

    def _do_send(self) -> None:
        if not self._alive(getattr(self, "send_addr", None)):
            return
        to_addr = self.send_addr.get().strip()
        amount = self.send_amount.get().strip()
        fee = self.send_fee.get().strip() or None
        if not to_addr or not amount:
            messagebox.showerror(APP_TITLE, "Address and amount are required.")
            return
        pwd = self._ask_password("Enter wallet password:")
        if not pwd:
            return
        try:
            txid = self.ctrl.send(to_addr, amount, pwd, fee_mhc=fee)
        except (WalletError, ValueError) as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        messagebox.showinfo(APP_TITLE, f"Transaction sent.\n\nTXID:\n{txid}")
        self.refresh_all()

    def _start_mine(self) -> None:
        addr = ""
        if self._alive(getattr(self, "mine_addr", None)):
            addr = self.mine_addr.get().strip()
        try:
            if not addr:
                addr = self.ctrl.default_address()
                if self._alive(getattr(self, "mine_addr", None)):
                    self.mine_addr.delete(0, tk.END)
                    self.mine_addr.insert(0, addr)
            self.ctrl.start_mining(addr, on_block=lambda *a: self.after(0, self.refresh_all))
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        if self._alive(getattr(self, "mine_status", None)):
            self.mine_status.configure(text="Status: Mining...")

    def _stop_mine(self) -> None:
        self.ctrl.stop_mining()
        if self._alive(getattr(self, "mine_status", None)):
            self.mine_status.configure(text="Status: Stopped")

    def _start_node(self) -> None:
        try:
            self.ctrl.start_node()
            messagebox.showinfo(APP_TITLE, "Node started in background.")
        except Exception as e:
            messagebox.showerror(APP_TITLE, str(e))
        self.refresh_all()

    def _stop_node(self) -> None:
        self.ctrl.stop_node()
        self.refresh_all()

    def refresh_all(self) -> None:
        if self._screen != "main":
            return
        try:
            addr = self.ctrl.default_address()
        except WalletError:
            addr = None

        if self._alive(getattr(self, "balance_lbl", None)):
            self.balance_lbl.configure(text=self.ctrl.balance_text())
        if addr and self._alive(getattr(self, "recv_addr", None)):
            self.recv_addr.configure(state="normal")
            self.recv_addr.delete("1.0", "end")
            self.recv_addr.insert("end", addr)
            self.recv_addr.configure(state="disabled")
        if addr and self._alive(getattr(self, "mine_addr", None)) and not self.mine_addr.get().strip():
            self.mine_addr.insert(0, addr)

        info = self.ctrl.chain_info()
        if self._alive(getattr(self, "overview_sync", None)):
            self.overview_sync.configure(
                text=f"{info['sync']} | Block: {info['height']:,} | Peers: {info['peers']}"
            )
        if self._alive(getattr(self, "status_lbl", None)):
            self.status_lbl.configure(text=f"{info['network']} | height {info['height']}")

        labels = getattr(self, "net_labels", None) or {}
        if labels:
            if self._alive(labels.get("network")):
                labels["network"].configure(text=f"Network: {info['network']}")
            if self._alive(labels.get("sync")):
                labels["sync"].configure(text=f"Status: {info['sync']}")
            if self._alive(labels.get("height")):
                labels["height"].configure(text=f"Block height: {info['height']:,}")
            if self._alive(labels.get("peers")):
                labels["peers"].configure(text=f"Peers: {info['peers']}")
            tip = info["tip"] or "-"
            if self._alive(labels.get("tip")):
                labels["tip"].configure(
                    text=f"Tip: {tip[:28]}..." if len(tip) > 28 else f"Tip: {tip}"
                )

        stats = self.ctrl.mining_stats
        if self._alive(getattr(self, "mine_status", None)):
            self.mine_status.configure(text="Status: Mining..." if stats["mining"] else "Status: Stopped")
        if self._alive(getattr(self, "mine_hash", None)):
            hr = stats["hashrate"]
            self.mine_hash.configure(text=f"Hashrate: {hr:,.0f} H/s" if hr else "Hashrate: -")
        if self._alive(getattr(self, "mine_blocks", None)):
            self.mine_blocks.configure(text=f"Blocks found: {stats['blocks_found']}")
        if self._alive(getattr(self, "mine_rewards", None)):
            self.mine_rewards.configure(text=f"Rewards: {stats['rewards_text']}")

        if self._alive(getattr(self, "settings_net", None)):
            self.settings_net.configure(text=f"Active network: {self.ctrl.network}")
        if self._alive(getattr(self, "settings_dir", None)):
            self.settings_dir.configure(text=f"Data directory: {self.ctrl.data_dir}")

        rows = self.ctrl.recent_transactions()
        if self._alive(getattr(self, "overview_list", None)):
            self.overview_list.delete(0, "end")
            for r in rows[:8]:
                sign = "+" if r.kind != "send" else "-"
                self.overview_list.insert("end", f"{sign}{format_mhc(r.amount_sats)} MHC    {r.kind}")
        if self._alive(getattr(self, "tx_list", None)):
            self.tx_list.delete(0, "end")
            for r in rows:
                self.tx_list.insert("end", f"{r.kind}  {format_mhc(r.amount_sats)} MHC  h={r.height}")

    def _tick(self) -> None:
        if not self._tick_on or self._screen != "main":
            return
        self.refresh_all()
        self.after(3000 if self.ctrl.is_mining else 2000, self._tick)

    def _on_close(self) -> None:
        self._tick_on = False
        self.ctrl.shutdown()
        self.destroy()


def main(argv: list[str] | None = None) -> None:
    os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
    argv = list(argv or sys.argv[1:])
    network = os.environ.get("MHCOIN_NETWORK", "localnet")
    if "--network" in argv:
        i = argv.index("--network")
        if i + 1 < len(argv):
            network = argv[i + 1]
    MhcoinDesktop(network=network).mainloop()


if __name__ == "__main__":
    main()
