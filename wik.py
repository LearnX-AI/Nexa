import wikipedia
from ddgs import DDGS  # Updated package
import time

def search_wikipedia(query: str, sentences: int = 5):
    """Search Wikipedia with better error handling"""
    try:
        wikipedia.set_lang("en")
        # Add user-agent to avoid blocking
        wikipedia.set_user_agent("NexaAI-EducationalBot/1.0")
        
        page_title = wikipedia.search(query, results=1)[0]
        summary = wikipedia.summary(page_title, sentences=sentences)
        return f"**📖 From Wikipedia:**\n{summary}"
    
    except Exception as e:
        return f"**Wikipedia:** Could not fetch information. Error: {str(e)[:100]}"


def web_search(query: str, max_results: int = 3):
    """Web Search using updated ddgs"""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            if not results:
                return "**🌐 Web Search:** No results found."
            
            output = "**🌐 Web Search Results:**\n\n"
            for i, r in enumerate(results, 1):
                output += f"{i}. **{r['title']}**\n{r['body'][:280]}...\n[Read more]({r['href']})\n\n"
            return output
    except Exception as e:
        return f"**Web Search:** Failed - {str(e)[:100]}"


# ==================== TEST ====================
if __name__ == "__main__":
    print("="*70)
    print("NEXA SEARCH TEST")
    print("="*70)

    tests = [
        ("Photosynthesis", "Wikipedia"),
        ("History of Papua New Guinea", "Wikipedia"),
        ("causes of climate change", "Web Search")
    ]

    for query, source in tests:
        print(f"\n🔍 Testing: {query}")
        if source == "Wikipedia":
            result = search_wikipedia(query)
        else:
            result = web_search(query)
        
        print(result)
        print("-" * 60)
        time.sleep(1)  # Be gentle with APIs

    print("\n✅ Test Completed!")
