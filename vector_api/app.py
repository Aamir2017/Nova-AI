import os
from flask import Flask, request, jsonify
import mysql.connector
from sentence_transformers import SentenceTransformer
import chromadb
import requests
import json

if not os.path.exists('static'):
    os.makedirs('static')

app = Flask(__name__, static_folder='static', static_url_path='/static')

print("Loading AI Model and syncing databases...")

# ── 1. PRODUCT DATABASE & VECTOR SYNC (UPGRADED FOR 8-TABLE SCHEMA) ──────
db = mysql.connector.connect(
    host="localhost", user="root", password="admin", database="ecommerce_db"
)
cursor = db.cursor(dictionary=True)
cursor.execute("""
    SELECT product_id, sku, name, brand, category, price, discount_percent,
           stock_quantity, rating, warranty_months, is_returnable,
           specs_json, description
    FROM products;
""")
products = cursor.fetchall()
cursor.close()
db.close()

chroma_client = chromadb.PersistentClient(path="./vector_store")
collection = chroma_client.get_or_create_collection(name="ecommerce_products")
model = SentenceTransformer('all-MiniLM-L6-v2')

# Always sync MySQL catalog to ChromaDB on startup so new schema data is live
if products:
    existing = collection.get()
    if existing and existing['ids']:
        collection.delete(ids=existing['ids'])

    docs, metas, ids = [], [], []
    for p in products:
        # Rich semantic text combining name, brand, category, and description
        rich_doc = f"{p['brand']} {p['name']} {p['category']} {p.get('description', '')}"
        docs.append(rich_doc)

        specs_str = p['specs_json'] if isinstance(p['specs_json'], str) else json.dumps(p['specs_json'] or {})

        metas.append({
            "product_id": int(p['product_id']),
            "sku": p['sku'],
            "name": p['name'],
            "brand": p['brand'],
            "category": p['category'],
            "price": float(p['price']),
            "discount_percent": int(p['discount_percent']),
            "stock": int(p['stock_quantity']),           # Backward-compatible key
            "stock_quantity": int(p['stock_quantity']),
            "rating": float(p['rating']),
            "warranty_months": int(p['warranty_months']),
            "is_returnable": bool(p['is_returnable']),
            "specs_json": specs_str
        })
        ids.append(str(p['product_id']))

    collection.upsert(documents=docs, metadatas=metas, ids=ids)
    print(f"✅ Successfully synced and embedded {len(products)} enterprise products!")



# ── 2. POLICY DOCUMENT INGESTION FOR RAG (Always sync latest policy.txt) ──
policy_collection = chroma_client.get_or_create_collection(name="policy_docs")

def ingest_policy_doc():
    try:
        with open("policy.txt", "r", encoding="utf-8") as f:
            text = f.read()

        chunks = [c.strip() for c in text.split("\n\n") if len(c.strip()) > 10]

        if chunks:
            existing = policy_collection.get()
            if existing and existing['ids']:
                policy_collection.delete(ids=existing['ids'])

            embeddings = model.encode(chunks).tolist()
            ids = [f"chunk_{i}" for i in range(len(chunks))]
            policy_collection.upsert(documents=chunks, embeddings=embeddings, ids=ids)
            print(f"✅ Successfully embedded {len(chunks)} policy chunks!")
    except FileNotFoundError:
        print("⚠️ policy.txt not found. Create it to enable RAG FAQs.")

ingest_policy_doc()


# ── 3. ENDPOINTS ──────────────────────────────────────────────────────────

@app.route('/search', methods=['GET'])
def search():
    query = request.args.get('q')
    if not query:
        return jsonify([])

    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    brand = request.args.get('brand', type=str)

    query_vector = model.encode(query).tolist()
    results = collection.query(query_embeddings=[query_vector], n_results=11)

    items = results['metadatas'][0] if results['metadatas'] else []
    distances = results['distances'][0] if results.get('distances') else [0.0] * len(items)

    # 1. Expanded Category Synonyms (Supports single & multi-item queries)
    q_lower = query.lower()
    target_categories = set()
    if any(w in q_lower for w in ["monitor", "display", "screen", "ultragear", "odyssey", "proart"]):
        target_categories.add("monitors")
    if any(w in q_lower for w in ["keyboard", "keeb", "typing", "keychron", "mx keys", "wrist"]):
        target_categories.add("keyboards")
    if any(w in q_lower for w in ["mouse", "mice", "g502", "mx master"]):
        target_categories.add("mice")
    if any(w in q_lower for w in ["headphone", "earphone", "earbud", "earpod", "headset", "neckband", "tws", "buds", "speaker", "audio", "music", "soundcore", "xm5"]):
        target_categories.add("audio")
    if any(w in q_lower for w in ["ssd", "storage", "nvme", "drive", "memory", "990 pro"]):
        target_categories.add("storage")
    if any(w in q_lower for w in ["hub", "dock", "adapter", "usb-c", "charger", "port", "powerexpand"]):
        target_categories.add("accessories")

    # 2. Semantic Distance Guardrail (Prevents unrelated items from padding the top 5)
    best_distance = distances[0] if distances else 0.0

    filtered_items = []
    for item, dist in zip(items, distances):
        p = float(item.get('price', 0.0))
        b = item.get('brand', '')
        c = item.get('category', '').lower()

        if min_price is not None and p < min_price:
            continue
        if max_price is not None and p > max_price:
            continue
        if brand and brand.lower() not in b.lower():
            continue

        if target_categories:
            # Strict category match when category keywords are detected
            if c not in target_categories:
                continue
        else:
            # Open-ended semantic search (e.g. "gear for competitive gaming"):
            # Drop items that are semantically far away so unrelated items don't pad the list
            if dist > 1.45 or (dist - best_distance) > 0.35:
                continue

        filtered_items.append(item)

    return jsonify(filtered_items[:5])


@app.route('/faq', methods=['GET'])
def faq():
    query = request.args.get('q')
    if not query:
        return jsonify({"error": "No query provided"}), 400

    query_vector = model.encode(query).tolist()
    results = policy_collection.query(query_embeddings=[query_vector], n_results=2)

    if not results['documents'] or not results['documents'][0]:
        return jsonify({"answer": "I couldn't find any policy information."})

    context = "\n\n".join(results['documents'][0])

    prompt = (
        "You are Nova's official e-commerce policy assistant.\n"
        "Use the CONTEXT below to answer the customer's QUESTION directly in 1 to 2 clear sentences.\n"
        "Do not preface your answer with disclaimers if the policy is mentioned in the context.\n\n"
        f"CONTEXT:\n{context}\n\n"
        f"QUESTION: {query}\n"
        "DIRECT ANSWER:"
    )

    try:
        ollama_response = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": "phi3", "prompt": prompt, "stream": False},
            timeout=60
        )
        answer = ollama_response.json().get("response", "Error generating response.")
        return jsonify({"answer": answer.strip()})
    except requests.exceptions.RequestException:
        return jsonify({"answer": "Local LLM is offline. Ensure Ollama is running."}), 500

if __name__ == '__main__':
    print("Vector Search API running on http://127.0.0.1:5000")
    app.run(port=5000)