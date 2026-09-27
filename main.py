"""Local entrypoint: runs the servicing API against a JSON file store."""
import uvicorn

from loan_ledger.api import create_app
from loan_ledger.service import LoanServicingService
from loan_ledger.storage import Storage

storage = Storage("data/ledger.json")
service = LoanServicingService(storage, business_tz="Asia/Shanghai")
app = create_app(service)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
