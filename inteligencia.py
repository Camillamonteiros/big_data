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


async def pegar_text_or_none(locator, timeout=1500):
    try:
        return (await locator.inner_text(timeout=timeout)).strip()
    except:
        return None


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
                    if preco:
                        preco = f"R$ {preco}"

                # === Trecho solicitado (detecção de loja e quantidade vendida) ===
                if not loja:
                    loja = await pegar_text_or_none(detail.locator("a.ui-pdp-seller__link"))
                if not loja:
                    loja = await pegar_text_or_none(detail.locator("p.ui-pdp-seller_title, span.ui-pdp-seller_title"))
                if not loja:
                    loja = await pegar_text_or_none(detail.locator("p.ui-pdp-seller_status, span.ui-pdp-seller_status"))
                if not loja:
                    loja_element = detail.locator("span.ui-pdp-seller__label-text-with-icon")
                    if await loja_element.count() > 0:
                        loja = await loja_element.first.evaluate(
                            "element => element.childNodes[0].textContent.trim()"
                        )

                qtd_vendida = await pegar_text_or_none(detail.locator(
                    "div.ui-pdp-seller_headerinfo-containersubtitle-one-line p.ui-pdp-sellerheader_subtitle"
                ))
                if not qtd_vendida:
                    qtd_vendida = await pegar_text_or_none(detail.locator(".ui-pdp-subtitle"))
                qtd_vendida = qtd_vendida or "Não informado"
                # =============================================================

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


def salvar_csv(produtos, arquivo):
    campos = ["concorrente", "Preço", "Loja", "qtd_vendida", "link", "principal"]
    with open(arquivo, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(produtos)
    print(f"✅ CSV salvo: {arquivo}")


def salvar_excel(produtos, arquivo):
    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos"
    campos = ["concorrente", "Preço", "Loja", "qtd_vendida", "link", "principal"]
    ws.append(campos)
    for p in produtos:
        ws.append([p[c] for c in campos])
    wb.save(arquivo)
    print(f"✅ Excel salvo: {arquivo}")


# === FUNÇÕES DE COMPARAÇÃO COM IA ===
def extrair_compatibilidade(conteudo):
    conteudo_upper = conteudo.upper()
    if re.search(r'\bSIM\b', conteudo_upper):
        return "SIM"
    else:
        return "NÃO"


def comparar_com_ia(principal, concorrente):
    prompt = f"""
Compare os dois produtos abaixo e diga se são compatíveis ou não.
Leve em consideração nome, marca, cor, unidade, voltagem (se houver) e ano do produto. 
Só aceite caso os parâmetros indicados combinem e tenham uma compatibilidade de no mínimo 97%.

Produto principal: {principal}
Produto concorrente: {concorrente}

Formato da resposta:
Compatibilidade: SIM ou NÃO
Justificativa: texto explicativo breve.
"""
    response = client.chat.completions.create(
        model=MODELO,
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )

    conteudo = response.choices[0].message.content.strip()
    compatibilidade = extrair_compatibilidade(conteudo)

    print(f"\n🧠 Comparação:\nPrincipal: {principal}\nConcorrente: {concorrente}\n👉 {conteudo}\n")

    return compatibilidade


def aplicar_ia_csv(arquivo_csv):
    df = pd.read_csv(arquivo_csv)
    df["compatibilidade"] = df.apply(
        lambda row: comparar_com_ia(row["principal"], row["concorrente"]), axis=1
    )

    df.to_csv(RESULTADO_CSV, index=False, encoding="utf-8-sig")
    df.to_excel(RESULTADO_XLSX, index=False)
    print(f"\n✅ Resultados salvos:\n- {RESULTADO_CSV}\n- {RESULTADO_XLSX}")


# === EXECUÇÃO PRINCIPAL ===
if __name__ == "__main__":
    termo = "Smart Tv LG 50 4k Uhd Hdr Thinq Ai Pro Wi-fi Bluetooth Alexa Apple Airplay - 50tu801c0sa"
    produtos = asyncio.run(scrape_mercado_livre(termo, max_itens=5))
    salvar_csv(produtos, OUTPUT_CSV)
    salvar_excel(produtos, OUTPUT_XLSX)

    print("\n🚀 Iniciando comparações com IA...")
    aplicar_ia_csv(OUTPUT_CSV)