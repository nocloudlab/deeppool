import os
import sys

# Must be set before app.py is imported: skips init_db() and the
# background polling threads, so importing the module is side-effect
# free in tests.
os.environ["ZFS_MONITOR_NO_POLL"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
