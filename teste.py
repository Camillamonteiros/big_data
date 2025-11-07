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

API_KEY = ""
MODELO = ""

client = Groq(api_key=API_KEY)


# === FUNÇÕES AUXILIARES ===

async def pegar_text_or_none(locator, timeout=2000):
    try:
        return (await locator.inner_text(timeout=timeout)).strip()
    except:
        return None


async def extrair_jsonld(detail_page):
    try:
        scripts = await detail_page.locator("script[type='application/ld+json']").evaluate_all(
            "nodes => nodes.map(n => n.textContent)"
        )
    except:
        return None

    for s in scripts:
        if not s:
            continue
        try:
            data = json.loads(s)
        except:
            continue

        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            typ = obj.get("@type") or obj.get("type") or ""
            if ("Product" in str(typ)) or ("offers" in obj) or ("price" in obj):
                return obj
    return None


# ✅ Função para capturar o produto oficial (Comprebel)
async def scrape_oficial_comprebel(url, produto):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        await page.goto(url, timeout=60000)

        titulo = await pegar_text_or_none(page.locator("h1.ui-pdp-title"))

        preco_meta = await page.locator('meta[itemprop="price"]').get_attribute("content")
        if preco_meta:
            preco = f"R$ {preco_meta}"
        else:
            preco = await pegar_text_or_none(page.locator("span.andes-money-amount__fraction"))
            preco = f"R$ {preco}" if preco else "Não encontrado"

        await browser.close()

        return {
            "concorrente": titulo,
            "Preço": preco,
            "Loja": "Comprebel (Oficial)",
            "qtd_vendida": "Oficial",
            "principal": produto,
            "link": url
        }


# ✅ Função para Capturar concorrentes
async def scrape_mercado_livre(produto, max_itens=10):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        url = f"https://lista.mercadolivre.com.br/{produto.replace(' ', '-')}"

        await page.goto(url, timeout=60000)
        await page.wait_for_selector("a.poly-component__title", timeout=30000)

        itens = await page.locator("a.poly-component__title").evaluate_all(
            "nodes => nodes.map(n => ({title: n.innerText.trim(), href: n.href}))"
        )

        itens = itens[:max_itens]
        resultados = []

        for it in itens:
            titulo_lista = it.get("title") or ""
            link = it.get("href")
            detail = await context.new_page()

            try:
                await detail.goto(link, timeout=60000)
                jsonld = await extrair_jsonld(detail)

                titulo = jsonld.get("name") if jsonld else titulo_lista
                preco = None
                loja = None
                qtd_vendida = None

                if jsonld:
                    offers = jsonld.get("offers") or {}
                    if isinstance(offers, dict):
                        preco = offers.get("price")
                        seller = offers.get("seller") or {}
                        loja = seller.get("name") if isinstance(seller, dict) else None

                if not preco:
                    preco = await pegar_text_or_none(detail.locator("span.price-tag-fraction"))
                    preco = f"R$ {preco}" if preco else None

                if not loja:
                    loja = await pegar_text_or_none(detail.locator("a.ui-pdp-seller__link," "span.ui-pdp-seller__label-text-with-icon," "span.andes-money-amount__fraction"))

                qtd_vendida = await pegar_text_or_none(
                    detail.locator(".ui-pdp-subtitle")
                ) or "Não informado"

                resultados.append({
                    "concorrente": titulo,
                    "Preço": preco,
                    "Loja": loja,
                    "qtd_vendida": qtd_vendida,
                    "principal": produto,
                    "link": link
                })
            finally:
                await detail.close()

        await browser.close()
        return resultados


# ✅ Salvamento CSV/Excel
def salvar_csv(produtos, arquivo):
    campos = ["concorrente", "Preço", "Loja", "qtd_vendida", "link", "principal"]
    with open(arquivo, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(produtos)


def salvar_excel(produtos, arquivo):
    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos"
    campos = ["concorrente", "Preço", "Loja", "qtd_vendida", "link", "principal"]
    ws.append(campos)
    for p in produtos:
        ws.append([p[c] for c in campos])
    wb.save(arquivo)


# ✅ IA
def extrair_compatibilidade(conteudo):
    return "SIM" if re.search(r'\bSIM\b', conteudo.upper()) else "NÃO"


def comparar_com_ia(principal, concorrente):
    prompt = f"""
Compare os dois produtos abaixo e diga somente SIM ou NÃO.
Exija pelo menos 97% de similaridade.

Produto principal: {principal}
Produto concorrente: {concorrente}
"""

    try:
        resp = client.chat.completions.create(
            model=MODELO,
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )

        conteudo = resp.choices[0].message.content.strip()
        return "SIM" if "SIM" in conteudo.upper() else "NÃO"
    except Exception as e:
        print(f"⚠️ Erro IA: {e}")
        return "NÃO"

def aplicar_ia_csv(arquivo_csv):
    df = pd.read_csv(arquivo_csv)
    df["compatibilidade"] = df.apply(
        lambda row: comparar_com_ia(row["principal"], row["concorrente"]), axis=1
    )

    df.to_csv(RESULTADO_CSV, index=False, encoding="utf-8-sig")
    df.to_excel(RESULTADO_XLSX, index=False)


# ================= EXECUÇÃO =====================
if __name__ == "__main__":
    produto = "Smart Tv LG 50 4k Uhd Hdr Thinq Ai Pro Wi-fi Bluetooth Alexa Apple Airplay - 50tu801c0sa"
    link_oficial = "https://www.mercadolivre.com.br/smart-tv-lg-50-4k-uhd-hdr-thinq-ai-pro-wi-fi-bluetooth-alexa-apple-airplay-50tu801c0sa/p/MLB52058084?pdp_filters=official_store%3A1614"

    oficial = asyncio.run(scrape_oficial_comprebel(link_oficial, produto))
    concorrentes = asyncio.run(scrape_mercado_livre(produto, max_itens=20))

    produtos = [oficial] + concorrentes

    salvar_csv(produtos, OUTPUT_CSV)
    salvar_excel(produtos, OUTPUT_XLSX)

    print("\n🚀 Rodando IA para comparação...")
    aplicar_ia_csv(OUTPUT_CSV)
    print("✅ Finalizado!")
