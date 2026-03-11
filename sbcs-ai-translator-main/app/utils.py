"""
utils.py - Module untuk proyek
"""

import uuid
def new_job_id() -> str:
    return uuid.uuid4().hex[:16]
