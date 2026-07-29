# Live Pinecone free-tier test report

## Result

**PASS**

The following real cloud path was tested:

```text
Flask tool endpoint
  -> Mem0 OSS
  -> local Ollama nomic-embed-text embedding
  -> Pinecone serverless free-tier index
  -> semantic search
  -> three-bullet Human Agent handoff card
```

## Verified resources

- Backend: `mem0-ollama-pinecone`
- Pinecone index: `quicktalk-mem0-free`
- Test organization namespace: `org-pinecone-live-test`
- OpenAI usage: none
- Memory saved: yes
- Semantic search result: yes
- Three-bullet handoff card: yes

## Re-run

The API key remains only in the ignored local `.env` file.

```powershell
python pinecone_integration_test.py
```

The integration test never prints the API key.
