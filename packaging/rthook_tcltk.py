"""Set TCL/TK library paths inside the frozen MHCOIN Core bundle."""
import os
import sys

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    tcl = os.path.join(base, "tcl8.6")
    tk = os.path.join(base, "tk8.6")
    if os.path.isdir(tcl):
        os.environ.setdefault("TCL_LIBRARY", tcl)
    if os.path.isdir(tk):
        os.environ.setdefault("TK_LIBRARY", tk)
