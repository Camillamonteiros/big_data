import asyncio
import csv
import json
from pathlib import Path
from playwright.async_api import async_playwright
from openpyxl import Workbook
import pandas as pd
from groq import Groq
import re
from datetime import datetime

# === CONFIGURAÇÕES ===
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_CSV = OUTPUT_DIR / "dados.csv"
OUTPUT_XLSX = OUTPUT_DIR / "dados.xlsx"
RESULTADO_CSV = OUTPUT_DIR / f"resultado_final_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
RESULTADO_XLSX = OUTPUT_DIR / f"resultado_final_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

GROQ_API_KEY = "SUA_CHAVE_AQUI"
client = Groq(api_key=GROQ_API_KEY)


# === FUNÇÃO PARA LIMPAR STRINGS ===
def limpar(texto):
    if not texto:
        return ""
    return re.sub(r"\s+", " ", texto).strip()


# === EXTRAIR JSON-LD ===
def extrair_json_ld(html):
    try:
        padrao = r'<script type="application/ld\+json">(.*?)</script>'
        json_ld = re.findall(padrao, html, re.DOTALL)
        if json_ld:
            return json.loads(json_ld[0])
    except:
        pass
    return {}


# === SCRAPER PRINCIPAL ===
async def scrape_mercado_livre(url, playwright):

    browser = await playwright.chromium.launch(headless=True)  # NÃO ABRE JANELA
    page = await browser.new_page()

    try:
        await page.goto(url, timeout=60000)
        await page.wait_for_selector("a.poly-component__title", timeout=30000)

        # === SCROLL AUTOMÁTICO PARA CARREGAR MUITOS PRODUTOS ===
        previous_height = await page.evaluate("document.body.scrollHeight")

        for _ in range(40):  # CONFIGURE A QUANTIDADE DE SCROLLS
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1500)

            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == previous_height:
                break
            previous_height = new_height

        # === EXTRAIR LINKS ===
        itens = await page.locator("a.poly-component__title").evaluate_all(
            "nodes => nodes.map(n => ({title: n.innerText.trim(), href: n.href}))"
        )

        print(f"🔍 Itens encontrados: {len(itens)}")

        resultados = []

        for item in itens:
            try:
                produto_page = await browser.new_page()
                await produto_page.goto(item["href"], timeout=60000)

                html = await produto_page.content()
                json_ld = extrair_json_ld(html)

                titulo = limpar(json_ld.get("name", item["title"]))
                preco = json_ld.get("offers", {}).get("price", "")
                loja = json_ld.get("brand", {}).get("name", "")

                resultados.append({
                    "titulo": titulo,
                    "preco": preco,
                    "loja": loja,
                    "link": item["href"]
                })

                await produto_page.close()

            except Exception as e:
                print(f"Erro no item: {e}")

        await browser.close()
        return resultados

    except Exception as e:
        await browser.close()
        print(f"Erro geral na URL: {e}")
        return []


# === SALVAR CSV ===
def salvar_csv(dados):
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=dados[0].keys())
        writer.writeheader()
        writer.writerows(dados)


# === SALVAR EXCEL ===
def salvar_excel(dados):
    df = pd.DataFrame(dados)
    df.to_excel(OUTPUT_XLSX, index=False)


# === RODAR SCRAPER ===
async def main():
    url = input("Cole a URL de busca do Mercado Livre: ")

    async with async_playwright() as playwright:
        resultados = await scrape_mercado_livre(url, playwright)

    print(f"\n📦 Total coletado: {len(resultados)} produtos")

    if resultados:
        salvar_csv(resultados)
        salvar_excel(resultados)
        print("✅ Dados salvos com sucesso!")


if __name__ == "__main__":
    asyncio.run(main())
