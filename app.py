import codecs
import encodings.idna
import encodings.ascii
import encodings.utf_8

# Pre-register idna codec search hook to eliminate multithread LookupError on Render
def _idna_codec_search(encoding_name):
    if encoding_name.lower().replace("-", "_") == "idna":
        return encodings.idna.getregentry()
    return None

codecs.register(_idna_codec_search)

import os
from api.index import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
