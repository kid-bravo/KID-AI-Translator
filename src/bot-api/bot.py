async def _handle_attachments(self, turn_context: TurnContext, to_lang: str = "en"):
    att = turn_context.activity.attachments[0]
    name = att.name or f"file-{uuid.uuid4()}"
    content_url = att.content_url

    # 1) Download file
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(content_url)
            res.raise_for_status()
            file_bytes = res.content
    except Exception:
        await turn_context.send_activity("Gagal mengunduh file dari Teams. Coba drag-&-drop, bukan link.")
        return

    # 2) Upload ke blob input/
    bs = BlobServiceClient(
        account_url=f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=STORAGE_ACCOUNT_KEY
    )
    blob_name = f"{uuid.uuid4()}-{name}"
    bs.get_blob_client(container=STORAGE_CONTAINER_SOURCE, blob=blob_name).upload_blob(
        file_bytes, overwrite=True
    )

    # 3) SAS read untuk input/
    sas_read = generate_blob_sas(
        account_name=STORAGE_ACCOUNT_NAME,
        container_name=STORAGE_CONTAINER_SOURCE,
        blob_name=blob_name,
        account_key=STORAGE_ACCOUNT_KEY,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    )
    source_sas_url = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/{STORAGE_CONTAINER_SOURCE}/{blob_name}?{sas_read}"

    # 4) SAS write untuk output/<jobId>/
    job_id = str(uuid.uuid4())
    sas_write = generate_container_sas(
        account_name=STORAGE_ACCOUNT_NAME,
        container_name=STORAGE_CONTAINER_TARGET,
        account_key=STORAGE_ACCOUNT_KEY,
        permission=ContainerSasPermissions(write=True, add=True, create=True, list=True),
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=2)
    )
    target_sas_url = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/{STORAGE_CONTAINER_TARGET}?{sas_write}"

    # 5) Submit Document Translation batch
    batch_url = f"{TRANSLATOR_ENDPOINT}/translator/text/batch/v1.1/batches"
    headers = {
        "Ocp-Apim-Subscription-Key": TRANSLATOR_KEY,
        "Ocp-Apim-Subscription-Region": TRANSLATOR_REGION,
        "Content-Type": "application/json"
    }
    payload = {
        "inputs": [{
            "source": {"sourceUrl": source_sas_url},
            "targets": [{
                "targetUrl": f"{target_sas_url}/{job_id}",
                "language": to_lang
            }]
        }]
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(batch_url, headers=headers, json=payload)
        if r.status_code not in (201, 202):
            await turn_context.send_activity(f"Gagal submit job: {r.status_code}")
            return
        status_url = r.headers.get("Operation-Location")

    await turn_context.send_activity(f"Job diterima untuk {name}. Menunggu hasil…")

    # 6) Poll sampai selesai
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(30):
                s = await client.get(status_url, headers=headers)
                data = s.json()
                if data.get("status") in ("Succeeded", "Failed", "Cancelled"):
                    break
                await asyncio.sleep(3)

        if data.get("status") != "Succeeded":
            await turn_context.send_activity(f"Status job: {data.get('status')}")
            return

        # 7) Cari hasil di output/jobId/
        cc = bs.get_container_client(STORAGE_CONTAINER_TARGET)
        blobs = [b for b in cc.list_blobs(name_starts_with=f"{job_id}/")]

        if not blobs:
            await turn_context.send_activity("Job selesai tapi hasil tidak ditemukan.")
            return

        await turn_context.send_activity("Hasil terjemahan:")

        # kirim semua file hasil
        for b in blobs:
            sas_read_out = generate_blob_sas(
                account_name=STORAGE_ACCOUNT_NAME,
                container_name=STORAGE_CONTAINER_TARGET,
                blob_name=b.name,
                account_key=STORAGE_ACCOUNT_KEY,
                permission=BlobSasPermissions(read=True),
                expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=2)
            )
            url = (
                f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net/"
                f"{STORAGE_CONTAINER_TARGET}/{b.name}?{sas_read_out}"
            )
            await turn_context.send_activity(url)

    except Exception as e:
        await turn_context.send_activity(f"Error: {e}")
