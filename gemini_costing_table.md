# Costing Breakdown by Feature (Gemini 2.5 & Embedding)

Below is the structured cost table for **15,000 calls** showing where token consumption occurs and how much is spent per feature.

## Feature Costing Matrix

| Feature | Model Involved | Average Tokens per Call | Estimated Cost per Call | Total Cost for 15,000 Calls | Where the Tokens Go |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Welcome Message** | **Gemini 2.5 Flash** | 150 Input<br>25 Output | **$0.0001075** | **$1.613 USD** | Generates a personalized greeting when a customer initiates a chat session. |
| **2. Memory Retain** | **Gemini Embedding** | 30 Input (Embedding) | <ul><li>**$0.0000003** (at \$0.01/1M)</li><li>**$0.0000045** (at \$0.15/1M)</li></ul> | <ul><li>**$0.005 USD** (at \$0.01/1M)</li><li>**$0.068 USD** (at \$0.15/1M)</li></ul> | Converts incoming customer messages into vector embeddings to store in Pinecone. |
| **3. Handoff Profile** *(Standard)* | **None** *(Local SQL)* | 0 | **$0.0000000** | **$0.000 USD** | Renders profiles directly from precomputed SQLite tables (using fast indices & local RegEx). |
| **3. Handoff Profile** *(LLM Fallback)* | **Gemini 2.5 Flash** | 500 Input<br>100 Output | **$0.0004000** | **$6.000 USD** | Optional summary generation of the customer's entire 30-day history (Scenario B). |
| **4. Analytics Dashboard** | **None** *(Local SQL)* | 0 | **$0.0000000** | **$0.000 USD** | Queries database aggregates and sentiment analytics locally. |

---

## Detailed Mathematics & Token Pricing

### Pricing Parameters
* **Gemini 2.5 Flash**: Input = **$0.30 / 1M tokens** ($0.00000030/token) \| Output = **$2.50 / 1M tokens** ($0.00000250/token)
* **Embedding Model (Target)**: **$0.01 / 1M tokens** ($0.00000001/token)
* **Embedding Model (Standard)**: **$0.15 / 1M tokens** ($0.00000015/token)

### Formula Calculations

#### A. Welcome Message (Per Call)
$$\text{Cost per Call} = (150 \text{ Input} \times \$0.00000030) + (25 \text{ Output} \times \$0.00000250)$$
$$\text{Cost per Call} = \$0.0000450 + \$0.0000625 = \$0.0001075$$
$$\text{Total for 15,000 Calls} = 15,000 \times \$0.0001075 = \$1.6125 \approx \$1.61$$

#### B. Memory Retain (Per Call)
* **At $0.01 / 1M Target Rate**:
  $$\text{Cost per Call} = 30 \text{ Tokens} \times \$0.00000001 = \$0.0000003$$
  $$\text{Total for 15,000 Calls} = 15,000 \times \$0.0000003 = \$0.0045 \approx \$0.005$$
* **At $0.15 / 1M Standard Rate**:
  $$\text{Cost per Call} = 30 \text{ Tokens} \times \$0.00000015 = \$0.0000045$$
  $$\text{Total for 15,000 Calls} = 15,000 \times \$0.0000045 = \$0.0675 \approx \$0.07$$

#### C. Handoff Profile LLM Fallback (Per Call)
$$\text{Cost per Call} = (500 \text{ Input} \times \$0.00000030) + (100 \text{ Output} \times \$0.00000250)$$
$$\text{Cost per Call} = \$0.0001500 + \$0.0002500 = \$0.0004000$$
$$\text{Total for 15,000 Calls} = 15,000 \times \$0.0004000 = \$6.00$$
