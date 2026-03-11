from office365.runtime.auth.user_credential import UserCredential
from office365.sharepoint.client_context import ClientContext
from office365.sharepoint.files.file import File
import os


def download_sharepoint_method1():
    """Download menggunakan Office365 REST Client"""
    
    # SharePoint site URL (ganti dengan site URL yang sesuai)
    site_url = "https://synnexmetrodataindonesia-my.sharepoint.com/personal/bayuzen_ahmad_synnexmetrodataindonesia_onmicrosoft_com"
    
    # Credentials
    username = "Bayuzen.Ahmad@SynnexMetrodataIndonesia.onmicrosoft.com"  # Ganti dengan email Anda
    password = "Datazen19@"  # Ganti dengan password Anda
    
    # File path di SharePoint
    file_url = "/personal/bayuzen_ahmad_synnexmetrodataindonesia_onmicrosoft_com/_layouts/15/Doc.aspx?sourcedoc=%7B0E4B15DA-216E-4C64-9BBC-323FD2B21984%7D&file=PPTX_11MB_English%201__20251020-113325_zh-hant__20251021-135146_fr.pptx"
    
    # Nama file untuk disimpan
    download_path = "downloaded_presentation.pptx"
    
    try:
        # Authenticate
        ctx = ClientContext(site_url).with_credentials(UserCredential(username, password))
        
        # Download file
        with open(download_path, "wb") as local_file:
            file = ctx.web.get_file_by_server_relative_url(file_url)
            file.download(local_file)
            ctx.execute_query()
            
        print(f"File berhasil didownload: {download_path}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    download_sharepoint_method1()