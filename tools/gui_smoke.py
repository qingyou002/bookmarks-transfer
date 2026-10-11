"""Launch the real Tk window and check layout/lifecycle without browser changes."""
import json
import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from browser_transfer.gui import App

root = tk.Tk()
app = App(root)
root.update_idletasks()
root.update()
source, target = app._profiles()
assert (source.browser, target.browser) == ("Edge", "Chrome")
app.direction.set("Chrome")
app._direction_changed()
source, target = app._profiles()
assert (source.browser, target.browser) == ("Chrome", "Edge")
assert len(app.table.get_children()) == 5
assert all(variable.get() for variable in app.selected.values())
assert root.winfo_width() >= 800
assert app.start_button.winfo_ismapped()
assert app.table.winfo_ismapped()
print(json.dumps({"gui": "passed", "direction_selection": "both", "rows": 5,
                  "size": [root.winfo_width(), root.winfo_height()]}, ensure_ascii=False))
root.destroy()
