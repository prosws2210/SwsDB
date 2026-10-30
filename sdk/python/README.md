# swsdb (Python)

Client and command line for [swsdb](https://github.com/prosws2210/SwsDB).

```bash
pip install swsdb
swsdb status
```

```python
from swsdb import SwsDB

db = SwsDB("http://localhost:6464", api_key="...")
docs = db.create_collection("docs", vector_size=3)
docs.upsert([{"id": "a", "vector": [0.1, 0.2, 0.3], "payload": {"team": "support"}}])
print(docs.search([0.1, 0.2, 0.3], limit=5, where={"team": "support"}))
print(docs.cypher("CREATE (n:Note {body: $body}) RETURN n", {"body": "hello"}))
print(db.sql("SELECT now()"))
```
