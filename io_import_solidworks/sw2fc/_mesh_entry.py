# SPDX-License-Identifier: LGPL-2.1-or-later
# SolidWorks Import for Blender - https://github.com/djbarrow/blender_solidworks_plugin
# Written by Denis Joseph Barrow <barrow_dj@yahoo.com> with the help of Claude Code.

# Executed by freecadcmd for the Blender add-on; the job file path arrives in SW2FC_JOB.
import os
import sys

_here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _here not in sys.path:
    sys.path.insert(0, _here)

try:
    from sw2fc.mesh import main as _main  # noqa: E402
    _main()
    print("SW2FC_RC=0", flush=True)
except Exception as _exc:
    import traceback
    traceback.print_exc()
    print("SW2FC_FATAL %s" % _exc, flush=True)
    print("SW2FC_RC=1", flush=True)
