"""Facts the sync scripts have to agree on.

sync-resolve.py rebuilds detect.py's CODE_EXTENSIONS from this list and
sync-check.py verifies the result against it. A disagreement between the two
loses an extension in silence: the resolver writes a line the checker is not
looking for, every check passes, and the file type stops being detected.
"""

from __future__ import annotations

# In the order they appear inside detect.py's CODE_EXTENSIONS literal.
GODOT_EXTENSIONS = (".gd", ".tscn", ".tres", ".godot")
