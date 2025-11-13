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
COMPATIVEIS_CSV = OUTPUT_DIR / f"produtos_compativeis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
COMPATIVEIS_XLSX = OUTPUT_DIR / f"produtos_compativeis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

API_KEY = ""
MODELO = "openai/gpt-oss-20b"
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
        
        # Aguarda um seletor que indica que a página carregou
        await page.wait_for_selector("h1.ui-pdp-title", timeout=30000)

        titulo = await pegar_text_or_none(page.locator("h1.ui-pdp-title"))

        # Múltiplas estratégias para capturar o preço
        preco_oficial = None
        
        # Estratégia 1: Meta tag
        try:
            preco_meta = await page.locator('meta[itemprop="price"]').get_attribute("content", timeout=5000)
            if preco_meta:
                preco_oficial = f"R$ {preco_meta}"
        except:
            pass

        # Estratégia 2: Span com classe de preço
        if not preco_oficial:
            try:
                preco_span = await pegar_text_or_none(page.locator("span.andes-money-amount__fraction"))
                if preco_span:
                    preco_oficial = f"R$ {preco_span}"
            except:
                pass

        # Estratégia 3: Outros seletores comuns de preço
        if not preco_oficial:
            try:
                preco_selectors = [
                    "span.ui-pdp-price__part",
                    "div.ui-pdp-price__main-container",
                    "span.price-tag-fraction",
                    "meta[property='product:price:amount']"
                ]
                for selector in preco_selectors:
                    if selector.startswith("meta"):
                        preco_val = await page.locator(selector).get_attribute("content", timeout=2000)
                    else:
                        preco_val = await pegar_text_or_none(page.locator(selector))
                    
                    if preco_val:
                        preco_oficial = f"R$ {preco_val}" if not preco_val.startswith("R$") else preco_val
                        break
            except:
                pass

        # Estratégia 4: Buscar no JSON-LD
        if not preco_oficial:
            try:
                jsonld = await extrair_jsonld(page)
                if jsonld:
                    offers = jsonld.get("offers") or {}
                    if isinstance(offers, dict):
                        preco_ld = offers.get("price")
                        if preco_ld:
                            preco_oficial = f"R$ {preco_ld}"
            except:
                pass

        # Se nenhuma estratégia funcionou
        if not preco_oficial:
            preco_oficial = "Preço não encontrado"

        await browser.close()

        return {
            "concorrente": titulo,
            "Preço": preco_oficial,  # Usa o mesmo preço encontrado
            "preço_oficial": preco_oficial,
            "Loja": "Comprebel (Oficial)",
            "qtd_vendida": "Oficial",
            "principal": produto,
            "link": url
        }


# ✅ Função para Capturar concorrentes
async def scrape_mercado_livre(produto, max_itens=10, preco_oficial=None):
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

                # ✅ Hierarquia de seletores para capturar a loja
                if not loja:
                    loja = await pegar_text_or_none(detail.locator("a.ui-pdp-seller__link"))
                if not loja:
                    loja = await pegar_text_or_none(detail.locator("p.ui-pdp-seller__title, span.ui-pdp-seller__title"))
                if not loja:
                    loja = await pegar_text_or_none(detail.locator("p.ui-pdp-seller__status, span.ui-pdp-seller__status"))
                if not loja:
                    loja_element = detail.locator("span.ui-pdp-seller__label-text-with-icon")
                    if await loja_element.count() > 0:
                        loja = await loja_element.first.evaluate(
                            "element => element.childNodes[0].textContent.trim()"
                        )

                qtd_vendida = await pegar_text_or_none(
                    detail.locator(".ui-pdp-subtitle")
                ) or "Não informado"

                resultados.append({
                    "concorrente": titulo,
                    "Preço": preco,
                    "preço_oficial": preco_oficial,
                    "Loja": loja,
                    "qtd_vendida": qtd_vendida,
                    "principal": produto,
                    "link": link
                })
            except Exception as e:
                print(f"⚠️ Erro ao processar item: {e}")
            finally:
                await detail.close()

        await browser.close()
        return resultados


# ✅ Salvamento CSV/Excel
def salvar_csv(produtos, arquivo):
    campos = ["ranking", "concorrente", "Preço", "preço_oficial", "preço_indicado", "Loja", "qtd_vendida", "link", "principal"]
    with open(arquivo, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(produtos)


def salvar_excel(produtos, arquivo):
    wb = Workbook()
    ws = wb.active
    ws.title = "Produtos"
    campos = ["ranking", "concorrente", "Preço", "preço_oficial", "preço_indicado", "Loja", "qtd_vendida", "link", "principal"]
    ws.append(campos)
    for p in produtos:
        ws.append([p[c] for c in campos])
    wb.save(arquivo)


# ✅ FUNÇÃO PARA CRIAR RANKING E PREÇO INDICADO
def aplicar_ranking_e_preco_indicado(df):
    # Filtrar apenas produtos compatíveis
    df_compativel = df[df["compatibilidade"] == "SIM"].copy()
    
    # Converter preços para numérico para ordenação
    def extrair_valor_preco(preco_str):
        if pd.isna(preco_str) or preco_str is None:
            return float('inf')
        try:
            # Remove "R$", espaços e converte para float
            valor = re.sub(r'[^\d,]', '', str(preco_str))
            valor = valor.replace(',', '.')
            return float(valor)
        except:
            return float('inf')
    
    df_compativel['preco_numerico'] = df_compativel['Preço'].apply(extrair_valor_preco)
    
    # Ordenar por preço (menor para maior)
    df_compativel = df_compativel.sort_values('preco_numerico')
    
    # Adicionar ranking
    df_compativel['ranking'] = range(1, len(df_compativel) + 1)
    
    # Encontrar preço do terceiro colocado
    preco_terceiro = None
    if len(df_compativel) >= 3:
        preco_terceiro = df_compativel.iloc[2]['Preço']
    else:
        # Se não houver terceiro, usar o último disponível
        preco_terceiro = df_compativel.iloc[-1]['Preço'] if len(df_compativel) > 0 else "N/A"
    
    # Encontrar preço oficial (Comprebel)
    preco_oficial_df = df[df['Loja'] == 'Comprebel (Oficial)']
    preco_oficial_val = preco_oficial_df['Preço'].iloc[0] if len(preco_oficial_df) > 0 else "N/A"
    
    # Criar coluna preço_indicado
    preco_indicado = f"{preco_terceiro} (3º) | {preco_oficial_val} (Comprebel)"
    
    # Aplicar ranking e preço_indicado ao dataframe original
    df_final = df.copy()
    
    # Adicionar ranking baseado no df_compativel
    ranking_map = dict(zip(df_compativel.index, df_compativel['ranking']))
    df_final['ranking'] = df_final.index.map(ranking_map)
    
    # Preencher ranking para produtos não compatíveis
    df_final['ranking'] = df_final['ranking'].fillna('N/A')
    
    # Adicionar preço_indicado para todas as linhas
    df_final['preço_indicado'] = preco_indicado
    
    # Reordenar colunas
    colunas = ['ranking', 'concorrente', 'Preço', 'preço_oficial', 'preço_indicado', 
               'Loja', 'qtd_vendida', 'link', 'principal', 'compatibilidade']
    
    # Garantir que todas as colunas existam
    for col in colunas:
        if col not in df_final.columns:
            df_final[col] = 'N/A'
    
    return df_final[colunas]


# ✅ FUNÇÃO PARA CRIAR ARQUIVO APENAS COM COMPATÍVEIS
def criar_arquivo_compativeis(df):
    # Filtrar apenas produtos com compatibilidade = SIM
    df_compativeis = df[df["compatibilidade"] == "SIM"].copy()
    
    # Remover a coluna preço_oficial
    if 'preço_oficial' in df_compativeis.columns:
        df_compativeis = df_compativeis.drop(columns=['preço_oficial'])
    
    # Reordenar colunas para melhor visualização
    colunas_finais = ['ranking', 'concorrente', 'Preço', 'preço_indicado', 
                     'Loja', 'qtd_vendida', 'link', 'principal', 'compatibilidade']
    
    # Manter apenas colunas existentes
    colunas_finais = [col for col in colunas_finais if col in df_compativeis.columns]
    
    return df_compativeis[colunas_finais]


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
    
    # Aplicar ranking e preço indicado
    df_final = aplicar_ranking_e_preco_indicado(df)

    # Salvar arquivo completo com todos os produtos
    df_final.to_csv(RESULTADO_CSV, index=False, encoding="utf-8-sig")
    df_final.to_excel(RESULTADO_XLSX, index=False)
    
    # Criar e salvar arquivo apenas com produtos compatíveis
    df_compativeis = criar_arquivo_compativeis(df_final)
    df_compativeis.to_csv(COMPATIVEIS_CSV, index=False, encoding="utf-8-sig")
    df_compativeis.to_excel(COMPATIVEIS_XLSX, index=False)
    
    print(f"📊 Ranking aplicado! Preço indicado: {df_final['preço_indicado'].iloc[0]}")
    print(f"✅ Arquivo completo salvo: {RESULTADO_CSV}")
    print(f"🎯 Arquivo apenas compatíveis salvo: {COMPATIVEIS_CSV}")
    print(f"📈 Total de produtos compatíveis: {len(df_compativeis)}")


# ================= EXECUÇÃO =====================
if __name__ == "__main__":
    produto = "Smart Tv LG 50 4k Uhd Hdr Thinq Ai Pro Wi-fi Bluetooth Alexa Apple Airplay - 50tu801c0sa"
    link_oficial = "https://www.mercadolivre.com.br/smart-tv-lg-50-4k-uhd-hdr-thinq-ai-pro-wi-fi-bluetooth-alexa-apple-airplay-50tu801c0sa/p/MLB52058084?pdp_filters=official_store%3A1614"

    # Primeiro captura o produto oficial para obter o preço oficial
    oficial = asyncio.run(scrape_oficial_comprebel(link_oficial, produto))
    preco_oficial = oficial["preço_oficial"]
    
    print(f"💰 Preço oficial capturado: {preco_oficial}")
    
    # Depois busca os concorrentes, passando o preço oficial
    concorrentes = asyncio.run(scrape_mercado_livre(produto, max_itens=40, preco_oficial=preco_oficial))

    produtos = [oficial] + concorrentes

    # Adicionar colunas vazias para ranking e preço_indicado temporariamente
    for produto in produtos:
        produto['ranking'] = ''
        produto['preço_indicado'] = ''

    salvar_csv(produtos, OUTPUT_CSV)
    salvar_excel(produtos, OUTPUT_XLSX)

    print("\n🚀 Rodando IA para comparação e ranking...")
    aplicar_ia_csv(OUTPUT_CSV)
    print("✅ Finalizado!")