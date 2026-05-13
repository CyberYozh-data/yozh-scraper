# 📚 Examples - Yozh Scraper Usage Examples

Examples of using Yozh Scraper for various web scraping tasks.

## Quick start

1. Make sure that Yozh Scraper is running:
```bash
cd ../
poetry run python -m src.main
```

2. Run any example:
```bash
python examples/basic_scraping.py
```

## List of examples

### Basic examples
* **basic_scraping.py** - Simple scraping of one page
* **batch_scraping.py** - Scraping multiple pages at the same time
* **with_extraction_scraping.py** - Data extraction using CSS selectors

### Search engines
* **bing_search_scraping.py** - Scraping of Bing search results (first 20 pages)
* **pagination_scraping.py** - Pagination based on search results

### Screenshots
* **long_landing_screenshots_scraping.py** - Scraping a long landing page with full screenshots of pages

### AI & Chat
* **huggingface_chat_scraping.py** - Scrapping the free Huggingface chat interface
* **agent.py** - LangChain agent with MCP integration — chat interface that scrapes pages via yozh-scraper tools

### E-commerce
* **ecommerce_product_scraping.py** - Scraping product cards from books.toscrape.com

### Stealth (anti-bot sites)
* **stealth_amazon_scraping.py** - Amazon product page — need use stealth + residential proxy
* **stealth_ebay_scraping.py** - eBay search results — need use stealth + residential proxy

### Advanced
* **proxy_rotation_scraping.py** - IP rotation with residential rotating proxies
* **proxy_sticky_session_scraping.py** - Sticky sessions (res_static) and pool pinning
* **geo_scraping.py** - GEO targeting by country/city with IP verification
* **concurrent_batch_scraping.py** - Submit multiple pages in one job (new batch API)

## LangChain Agent (MCP)

`agent.py` connects to Yozh Scraper via MCP (Model Context Protocol) and exposes its tools to a LangChain ReAct agent. This allows you to interact with the scraper through a natural language chat interface.

**Run:**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
python examples/agent.py
```

**Example session:**
```
You: Scrape https://example.com and tell me what's on the page
Agent: The page is a placeholder domain maintained by IANA...
```

The agent automatically handles the full job lifecycle: submits the scrape request, polls `get_job_status` until done, fetches results via `get_job_result`, and summarizes the content.

## Setting up

### Installing dependencies
```bash
pip install -r requirements.txt
```

### Environment variables
Create a `.env` file in the root of the project:
```bash
# Open Scraper API
OPEN_SCRAPER_URL=http://localhost:8000

# CyberYozh Proxy (optional)
CYBERYOZH_API_KEY=your_api_key_here
```

## Usage
Each example is an independent script that can be run directly.:

```bash
python examples/bing_search_scraping.py
```

Or import functions:

```python
from examples.bing_search_scraping import scrape_bing_search

results = scrape_bing_search("Python web scraping", pages=5)
```

## Popular scenarios

### 1. Price monitoring
```python
from examples.ecommerce_product_scraping import scrape_books_product

product = scrape_books_product()
print(f"Price: {product['price']}")
```

### 2. SEO analysis

```python
from examples.bing_search_scraping import scrape_bing_search

results = scrape_bing_search("your keyword", pages=10)
for result in results:
    print(f"{result['position']}: {result['title']} - {result['url']}")
```

### 3. Screenshots for testing

```python
from examples.long_landing_screenshots_scraping import capture_landing_page

screenshots = capture_landing_page("https://example.com")
print(f"Saved {len(screenshots)} screenshots")
```

## Troubleshooting

### The "Connection refused" error
Make sure that Yozh Scraper is running:

```bash
poetry run python -m src.main
```

### The "Captcha detected" error

Use a proxy:
```bash
python examples/proxy_rotation_scraping.py
```
