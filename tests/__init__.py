"""tests 包：把工程根目录放进 sys.path，保证 `python -m unittest discover -s tests`
在任意工作目录下都能 import 到 mcap_reader / desktop 等模块。"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
