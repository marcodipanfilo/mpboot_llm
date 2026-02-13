from agents.enrichment import enrichment

if __name__ == "__main__":
    xml = enrichment("persons", limit_samples=5)
    print(xml)
