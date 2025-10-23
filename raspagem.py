import asyncio
import csv
import json
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from openpyxl import Workbook
import pandas as pd

# === Configurações ===
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OUTPUT_CSV = OUTPUT_DIR / "dados5.csv"
OUTPUT_XLSX = OUTPUT_DIR / "dados5.xlsx"

# === Funções existentes ===
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

async def scrape_mercado_livre(produto, max_itens=20):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        url = f"https://lista.mercadolivre.com.br/{produto.replace(' ', '-')}"
        await page.goto(url, timeout=60000)

        try:
            await page.wait_for_selector("a.poly-component__title", timeout=30000)
        except TimeoutError:
            print("Não encontrou elementos na página de listagem.")
            await browser.close()
            return []

        itens = await page.locator("a.poly-component__title").evaluate_all(
            "nodes => nodes.map(n => ({title: n.innerText.trim(), href: n.href}))"
        )

        if not itens:
            print("Nenhum item coletado na listagem.")
            await browser.close()
            return []

        itens = itens[:max_itens]
        resultados = []
        print(f"Itens para processar: {len(itens)} (limite {max_itens})")

        for idx, it in enumerate(itens, start=1):
            titulo_lista = it.get("title") or ""
            link = it.get("href")
            print(f"[{idx}/{len(itens)}] Acessando: {titulo_lista}")

            detail = await context.new_page()
            try:
                await detail.goto(link, timeout=60000)
                try:
                    await detail.wait_for_load_state("networkidle", timeout=10000)
                except:
                    pass

                jsonld = await extrair_jsonld(detail)
                titulo = None
                preco = None
                loja = None
                vendedor = None
                qtd_vendida = None

                if jsonld:
                    titulo = jsonld.get("name") or jsonld.get("headline") or titulo_lista
                    offers = jsonld.get("offers") or {}
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if isinstance(offers, dict):
                        preco = offers.get("price") or offers.get("priceSpecification", {}).get("price")
                        seller = offers.get("seller") or {}
                        if isinstance(seller, dict):
                            loja = seller.get("name") or seller.get("nickname")
                        else:
                            loja = seller or None

                if not titulo:
                    titulo = await pegar_text_or_none(detail.locator("h1, h1.ui-pdp-title, h1#productTitle")) or titulo_lista

                if not preco:
                    preco = await pegar_text_or_none(detail.locator("span.price-tag-fraction"))
                    if not preco:
                        preco = await pegar_text_or_none(detail.locator("span.andes-money-amount__fraction"))
                    cents = await pegar_text_or_none(detail.locator("span.price-tag-cents")) or await pegar_text_or_none(detail.locator("span.andes-money-amount__decimals"))
                    if preco and cents:
                        preco = f"R$ {preco},{cents}"
                    elif preco:
                        preco = f"R$ {preco}"

                if not loja:
                    loja = await pegar_text_or_none(detail.locator("a.ui-pdp-seller__link"))
                if not loja:
                    loja = await pegar_text_or_none(detail.locator("p.ui-pdp-seller_title, span.ui-pdp-seller_title"))
                if not loja:
                    loja = await pegar_text_or_none(detail.locator("p.ui-pdp-seller_status, span.ui-pdp-seller_status"))
                if not loja:
                # Para o span com a classe específica
                    loja_element = detail.locator("span.ui-pdp-seller__label-text-with-icon")
                if await loja_element.count() > 0:
                    # Pega apenas o texto, ignorando elementos filhos como imagens
                    loja = await loja_element.first.evaluate("element => element.childNodes[0].textContent.trim()")


                qtd_vendida = await pegar_text_or_none(detail.locator(
                    "div.ui-pdp-seller_headerinfo-containersubtitle-one-line p.ui-pdp-sellerheader_subtitle"
                ))
                if not qtd_vendida:
                    qtd_vendida = await pegar_text_or_none(detail.locator(".ui-pdp-subtitle"))
                qtd_vendida = qtd_vendida or "Não informado"

                resultados.append({
                    "Titulo": titulo,
                    "Preço": preco,
                    "Loja": loja,
                    "qtd_vendida": qtd_vendida,
                    "Oficial": "Smart Tv 32'' LG Hd 32LR600BPSA Processador 5 Ger6 Alexa Webos",
                    "link": link
                })

            except Exception as e:
                print(f"Erro ao processar {link}: {e}")
            finally:
                try:
                    await detail.close()
                except:
                    pass

        await browser.close()
        return resultados

def salvar_csv(produtos, arquivo=OUTPUT_CSV):
    campos = ["Titulo", "Preço", "Loja", "qtd_vendida", "link","Oficial"]
    with open(arquivo, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(produtos)
    print(f"✅ Salvou {len(produtos)} produtos em '{arquivo}'")

def salvar_excel(produtos, arquivo=OUTPUT_XLSX):
    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos"

    campos = ["Titulo", "Preço", "Loja", "qtd_vendida", "link","Oficial"]
    ws.append(campos)

    for p in produtos:
        ws.append([p[c] for c in campos])

    wb.save(arquivo)
    print(f"✅ Salvou {len(produtos)} produtos em '{arquivo}'")

# === Execução principal ===
if __name__ == "__main__":
    termo = "Smart Tv 32'' LG Hd 32LR600BPSA Processador 5 Ger6 Alexa Webos"
    produtos = asyncio.run(scrape_mercado_livre(termo, max_itens=20))

    print("\n📋 Produtos coletados:")
    for i, p in enumerate(produtos, start=1):
        print(f"{i}. {p['Titulo']} | Preço: {p['Preço']} | Loja: {p['Loja']} | Vendidos: {p['qtd_vendida']}")

    salvar_csv(produtos)
    salvar_excel(produtos)