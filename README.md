# Nova AI E-commerce Chatbot (v1.7)

AI e-commerce chatbot utilizing Rasa 3.x, a Python Flask Vector API, ChromaDB semantic search, and local Phi-3 Retrieval-Augmented Generation (RAG).

## Architecture
This project is contained in two main directories:
1. **Flask Vector API (`vector_api`)**: Handles product semantic search, RAG policy generation, and PDF report creation.
2. **Rasa Bot (`eCB_V1`)**: Manages conversational state, intent classification, and custom Python actions.

## Prerequisites
- Python 3.10+
- MySQL Server (Ensure the `ecommerce_db` is running on `localhost:3306`)
- Ollama (Install and download the local LLM: `ollama run phi3`)

## Setup Instructions

### 1. Global Setup (Dependencies)
From the root folder (`Nova-AI`), create a virtual environment and install the unified requirements for both the bot and the backend:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Flask Vector API Setup
1. Open a terminal, activate your virtual environment, and navigate to the `vector_api` directory.
2. Verify your MySQL database credentials in `app.py` (update `password` if necessary).
3. Start the Vector API Server:
   ```bash
   python app.py
   ```
   *(Note: On the first run, it will automatically embed the product catalog and `policy.txt` into a new ChromaDB `vector_store` folder).*

### 3. Rasa Bot Setup
1. Open a new terminal, activate the virtual environment, and navigate to the `eCB_V1` directory.
2. Train the Rasa machine learning model:
   ```bash
   rasa train
   ```
3. Start the Rasa Action Server (this handles the database interactions and business logic):
   ```bash
   rasa run actions
   ```
4. Open a third terminal, activate the virtual environment, navigate to `eCB_V1`, and start the Rasa API server to connect with the frontend:
   ```bash
   rasa run -m models --enable-api --cors "*"
   ```

## Key Features
- **Semantic Product Search**: Type "gear for competitive gaming" to see vector-based matches.
- **Local RAG Policy Assistant**: Ask complex store policy questions powered locally by Phi-3.
- **Smart Order Management**: Check status, securely cancel orders, and process 30-day validated returns.
- **Admin Commands**: Use "Download the daily stock and promo report" to generate a live PDF snapshot of the database.


**Database Schema**: Please refer to the database.md file for the exact MySQL tables required to run this project.