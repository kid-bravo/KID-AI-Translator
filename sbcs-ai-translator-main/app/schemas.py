"""
schemas.py - Module untuk proyek
"""

from pydantic import BaseModel

class JobCreateResp(BaseModel):
    job_id: str
    status: str

class JobStatusResp(BaseModel):
    job_id: str
    status: str
    message: str | None = None
    result_url: str | None = None
