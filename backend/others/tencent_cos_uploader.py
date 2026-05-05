import os
import mimetypes

from qcloud_cos import CosConfig, CosS3Client

# 本文件上传视频方法的调用示例
# record = get_video_record(db_path, args.user, video_name)
# video_url = record.get("video_url") if record else None
# object_key = record.get("video_object_key") if record else None
    
# uploader = TencentCosUploader()
# if not object_key:
#     upload_result = uploader.upload_file(video_path)
#     object_key = upload_result["key"]
#     upsert_user_video(
#         db_path,
#         user_id=args.user,
#         video_name=video_name,
#         fields={
#             "user_id": args.user,
#             "video_name": video_name,
#             "video_path": source_video_path,
#             "video_url": upload_result["url"],
#             "video_object_key": object_key,
#             "index_id": os.getenv("INDEX_ID"),
#             "status": "cos_uploaded",
#         },
#     )
# presign_expire = int(os.getenv("TENCENT_COS_PRESIGN_EXPIRE", "3600"))
# video_url = uploader.get_presigned_url(object_key, expired_in=presign_expire)

class TencentCosUploader:
    def __init__(self) -> None:
        secret_id = os.getenv("TENCENT_COS_SECRET_ID")
        secret_key = os.getenv("TENCENT_COS_SECRET_KEY")
        region = os.getenv("TENCENT_COS_REGION")
        bucket = os.getenv("TENCENT_COS_BUCKET")
        token = os.getenv("TENCENT_COS_SESSION_TOKEN", None)
        base_url = os.getenv("TENCENT_COS_BASE_URL", "")

        if not secret_id or not secret_key or not region or not bucket:
            raise ValueError("TENCENT_COS_* env vars not set; cannot upload.")

        self.bucket = bucket
        self.region = region
        self.prefix = (os.getenv("TENCENT_COS_PREFIX") or "lifelog").strip("/")
        self.base_url = base_url.strip().rstrip("/")

        config = CosConfig(
            Region=region,
            SecretId=secret_id,
            SecretKey=secret_key,
            Token=token,
            Scheme="https",
        )
        self.client = CosS3Client(config)

    def _default_key(self, local_path: str) -> str:
        base = os.path.basename(local_path)
        return f"{self.prefix}/{base}".replace("\\", "/")

    def upload_file(self, local_path: str, object_key: str | None = None) -> dict:
        key = (object_key or self._default_key(local_path)).lstrip("/")
        self.client.upload_file(
            Bucket=self.bucket,
            LocalFilePath=local_path,
            Key=key,
            EnableMD5=False,
        )
        url = self._build_url(key)
        return {"key": key, "url": url}

    def get_presigned_url(self, object_key: str, expired_in: int = 3600) -> str:
        key = object_key.lstrip("/")
        return self.client.get_presigned_url(
            Method="GET",
            Bucket=self.bucket,
            Key=key,
            ExpiredIn=expired_in,
        )

    def delete_object(self, object_key: str) -> None:
        key = object_key.lstrip("/")
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def _build_url(self, key: str) -> str:
        if self.base_url:
            return f"{self.base_url}/{key}"
        return f"https://{self.bucket}.cos.{self.region}.myqcloud.com/{key}"
