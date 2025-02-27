import os
import traceback
import boto3
import tempfile
from celery import shared_task
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.document_loaders import PyPDFLoader
from shared.models.opencopilot_db.pdf_data_sources import (
    insert_pdf_data_source,
    update_pdf_data_source_status,
)
from shared.utils.opencopilot_utils import (
    get_embeddings,
    StoreOptions,
    get_vector_store,
)
from utils.get_logger import CustomLogger
from workers.utils.remove_escape_sequences import remove_escape_sequences
from utils.llm_consts import STORAGE_TYPE, S3_BUCKET_NAME, SHARED_FOLDER

logger = CustomLogger(module_name=__name__)

embeddings = get_embeddings()
kb_vector_store = get_vector_store(StoreOptions("knowledgebase"))


def determine_file_storage_path(file_name):
    storage_type = os.getenv(
        "STORAGE_TYPE", "local"
    )  # Default to local storage if not specified
    if storage_type == "s3":
        if not S3_BUCKET_NAME:
            raise ValueError("S3_BUCKET_NAME environment variable is not set.")
        file_path = f"s3://{S3_BUCKET_NAME}/{file_name}"
        is_s3 = True
    else:
        file_path = os.path.join(SHARED_FOLDER, file_name)
        is_s3 = False
    return file_path, is_s3


def download_s3_file(bucket_name, s3_key):
    s3 = boto3.client("s3")
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        s3.download_file(bucket_name, s3_key, temp_file.name)
        return temp_file.name


@shared_task
def process_pdf(file_name: str, bot_id: str):
    try:
        file_path, is_s3 = determine_file_storage_path(file_name)
        # storing file path because the file can be in local or s3
        insert_pdf_data_source(chatbot_id=bot_id, file_name=file_path, status="PENDING")

        if is_s3:
            # Extract bucket name and key from the S3 URL
            s3_url = file_path
            bucket_name, s3_key = s3_url.replace("s3://", "").split("/", 1)
            # Download the file to a temporary location
            temp_file_path = download_s3_file(bucket_name, s3_key)
            # Use the local file path with PyPDFLoader
            loader = PyPDFLoader(temp_file_path)
        else:
            # Use the local file path directly with PyPDFLoader
            loader = PyPDFLoader(file_path)

        raw_docs = loader.load()

        # clean text
        for doc in raw_docs:
            logger.info("before_cleanup", page_content=doc.page_content)
            doc.page_content = remove_escape_sequences(doc.page_content)
            logger.info("after_cleanup", page_content=doc.page_content)

        # clean the data received from pdf document before passing it
        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000, chunk_overlap=200, length_function=len
        )
        docs = text_splitter.split_documents(raw_docs)

        for doc in docs:
            doc.metadata["bot_id"] = bot_id

        kb_vector_store.add_documents(docs)
        update_pdf_data_source_status(
            chatbot_id=bot_id, file_name=file_path, status="COMPLETED"
        )
    except Exception as e:
        traceback.print_exc()
        update_pdf_data_source_status(
            chatbot_id=bot_id, file_name=file_path, status="FAILED"
        )


@shared_task
def retry_failed_pdf_crawl(chatbot_id: str, file_name: str):
    """Re-runs a failed PDF crawl.

    Args:
      chatbot_id: The ID of the chatbot.
      file_name: The name of the PDF file to crawl.
    """

    update_pdf_data_source_status(
        chatbot_id=chatbot_id, file_name=file_name, status="PENDING"
    )
    try:
        process_pdf(file_name=file_name, bot_id=chatbot_id)
    except Exception as e:
        update_pdf_data_source_status(
            chatbot_id=chatbot_id, file_name=file_name, status="FAILED"
        )
        print(f"Error reprocessing {file_name}:", e)
