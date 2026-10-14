import encodings.idna
import encodings.ascii
import encodings.utf_8
import os
from api.index import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
