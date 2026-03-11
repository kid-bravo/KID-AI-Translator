import os
from dotenv import load_dotenv
#pandas
load_dotenv()

class Settings:
    # App
    APP_NAME = os.environ.get("APP_NAME", "sbcs-translator")
    APP_PORT = int(os.environ.get("APP_PORT", "8000"))
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "120"))
    CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")

    # Database (async SQLAlchemy URL)
    DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data.db")

    # Azure Storage
    AZURE_STORAGE_ACCOUNT_NAME = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    AZURE_STORAGE_ACCOUNT_KEY  = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
    AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    AZURE_STORAGE_BLOB_CONTAINER = os.environ.get("AZURE_STORAGE_BLOB_CONTAINER", "docs")
    AZURE_INPUT_CONTAINER  = os.getenv("AZURE_INPUT_CONTAINER", "input")
    AZURE_OUTPUT_CONTAINER = os.getenv("AZURE_OUTPUT_CONTAINER", "output")
    AZURE_STORAGE_QUEUE_NAME = os.environ.get("AZURE_STORAGE_QUEUE_NAME", "translation-jobs")

    # Azure Document Translation (Batch)
    AZURE_TRANSLATOR_ENDPOINT = os.environ.get("AZURE_TRANSLATOR_DOC_ENDPOINT", "").rstrip("/")
    AZURE_TRANSLATOR_KEY     = os.environ.get("AZURE_TRANSLATOR_KEY", "")
    AZURE_TRANSLATOR_REGION  = os.environ.get("AZURE_TRANSLATOR_REGION", "")
    DEFAULT_TARGET_LANG      = os.environ.get("DEFAULT_TARGET_LANG", "id")
    AZ_TENANT_ID             = os.environ.get("MSAL_TENANT_ID")
    AZ_APP_CLIENT_ID         = os.environ.get("MicrosoftAppId")
    AZ_APP_CLIENT_SECRET     = os.environ.get("MicrosoftAppPassword")
settings = Settings()
