# -*- coding: utf-8 -*-
"""Painel do Instagram — Instituto Cerrados (acesso restrito).

O Python busca as métricas ao vivo na API da Meta (Instagram Graph API) com o
token do usuário do sistema "coletametricasic" (app metricas_IC_ins,
portfólio Instituto Cerrados), guardado em st.secrets["META_TOKEN"], e entrega
tudo de uma vez para a interface em `painel.html` (HTML/JS com ECharts). A troca
de período, formato, aba e tema acontece no navegador, sem recarregar.

Login Google restrito aos e-mails de EMAILS_AUTORIZADOS, exigido quando o
bloco [auth] existe nos secrets (produção). Em dev local, sem esse bloco,
roda sem login.
"""

import json
import os
import pathlib
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests
import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

st.set_page_config(
    page_title="Painel do Instagram — Instituto Cerrados",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Quem pode abrir o painel. Para liberar para mais alguém do IC, acrescente
# o e-mail aqui e publique.
EMAILS_AUTORIZADOS = {"yuri@cerrados.org", "rebeca@cerrados.org"}

IG_USER_ID = "17841401645503865"
API = "https://graph.facebook.com/v21.0"
BASE = pathlib.Path(__file__).parent
ENV_LOCAL = pathlib.Path(r"C:\Users\yuri\.claude\projects\C--comunicacao_IC\metricas_instagram\.env")
BRASILIA = timezone(timedelta(hours=-3))

PERIODOS = [7, 30, 90, 180]
METRICAS_CONTA = "reach,views,accounts_engaged,total_interactions,likes,comments,shares,saves"
DIAS_HISTORICO = 2 * max(PERIODOS)

# Perfis de referência (aba "Referências"). Para trocar, edite esta lista com
# o @ exato de contas profissionais (comerciais ou de criador) do Instagram.
REFERENCIAS = [
    "ispn_brasil", "rede.cerrado", "cerradodepe", "funatura", "mapbiomasbrasil",
    "ipam_amazonia", "socioambiental", "observatoriodoclima", "wwfbrasil", "sosmataatlantica",
]
POSTS_POR_REFERENCIA = 30

# Classificação por palavras-chave (sem acento, minúsculas; casam como início
# de palavra). Um post pode ter mais de um tema e mais de uma abordagem.
TEMAS = {
    "Fogo e brigadas": ["fogo", "incendi", "queima", "brigad", "mif", "manejo integrado", "suindara", "foco de calor", "focos de"],
    "Água": ["agua!", "aguas!", "hidric", "recarga", "nascente", "chuva", "seca!", "secas!", "rio!", "rios!", "apcac", "apiac", "aprhic",
             "coracao das aguas", "elo das aguas", "sao francisco", "vereda", "aquifero"],
    "RPPN e áreas protegidas": ["rppn", "reserva particular", "mosaico", "unidade de conservacao", "unidades de conservacao",
                                "area protegida", "areas protegidas", "jurema", "parque nacional"],
    "Povos e comunidades": ["povos", "comunidades tradicionais", "comunidade tradicional", "indigena", "quilombo",
                            "quilombola", "kalunga", "pcts", "territorio", "acampamento terra livre"],
    "Biodiversidade": ["biodivers", "especie", "fauna", "flora", "lobo-guara", "tamandua", "tatu", "coruja",
                       "chuveirinho", "vida selvagem", "aves", "planta"],
    "Clima e carbono": ["clima", "climatic", "carbono", "el nino", "aquecimento", "cop30", "cop 30", "emisso"],
    "Educação ambiental": ["escola", "educa", "estudante", "aluno", "vivencia", "educador", "eixao", "oficina"],
    "Políticas e incidência": ["projeto de lei", "pl!", "congresso", "senado", "ministerio", "politica publica",
                               "politicas publicas", "governo", "jornal nacional", "codigo florestal", "desmatamento"],
    "Restauração": ["restaura", "semente", "exotica", "pinus", "eucalipto", "recuperacao", "plantio", "mudas!"],
    "Institucional": ["instituto cerrados completa", "15 anos", "nossa equipe", "equipe", "aniversario", "boas festas",
                      "retrospectiva", "nossa logo", "parcerias", "gratidao"],
}
TIPOS = {
    "Data comemorativa": ["dia mundial", "dia nacional", "dia internacional", "dia do cerrado", "dia da ", "dia do ", "dia dos "],
    "Bastidores e eventos": ["estivemos", "participamos", "esteve em", "esteve presente", "evento", "encontro",
                             "roda de conversa", "visita", "capacitacao", "reunimos", "ja estamos", "estaremos"],
    "Educativo (você sabia)": ["voce sabia", "voce sabe", "sabia que", "entenda", "saiba", "o que e", "o que sao", "explica"],
    "Chamada para ação": ["vote", "votou", "votar", "ajude", "sua ajuda", "inscreva", "participe", "link na bio",
                          "convida", "assine", "compartilhe"],
    "Conquista e anúncio": ["criada oficialmente", "criacao oficial", "reconhecido", "anunciamos", "conquista",
                            "finalista", "foi criada", "lancamento", "lancamos"],
    "Série e entrevista": ["serie", "entrevista", "neste video", "episodio", "presidente do", "diretor"],
}


def _normalizar(texto):
    t = unicodedata.normalize("NFD", (texto or "").lower())
    return " " + "".join(ch for ch in t if unicodedata.category(ch) != "Mn") + " "


def _ocorrencias(texto_norm, termos):
    """Conta termos que casam no início de palavra; "!" no fim exige palavra inteira."""
    total = 0
    for t in termos:
        inteira = t.endswith("!")
        padrao = r"(?<![a-z0-9])" + re.escape(t.rstrip("!")) + (r"(?![a-z0-9])" if inteira else "")
        total += len(re.findall(padrao, texto_norm))
    return total


# Frases fixas de rodapé que se repetem em quase todo post e distorceriam a
# classificação (ex.: convite para o canal de transmissão).
_RODAPES = [r"entre no nosso grupo de transmissao.*", r"baixe o suindara.*"]


def _pontuar(legenda, dicionario):
    """Peso 3 para o que aparece na abertura (primeiros 250 caracteres)."""
    t = _normalizar(legenda)
    for r in _RODAPES:
        t = re.sub(r, " ", t)
    abertura = t[:250]
    pontos = {nome: _ocorrencias(t, termos) + 2 * _ocorrencias(abertura, termos) for nome, termos in dicionario.items()}
    presentes = [n for n, p in sorted(pontos.items(), key=lambda kv: -kv[1]) if p > 0]
    return (presentes[0] if presentes else "Outros"), (presentes or ["Outros"])


def classificar(legenda):
    """Devolve (tema principal, todos os temas, abordagem principal, todas as abordagens)."""
    tema, temas = _pontuar(legenda, TEMAS)
    tipo, tipos = _pontuar(legenda, TIPOS)
    return tema, temas, tipo, tipos


def atributos(legenda):
    bruto = legenda or ""
    primeira = re.split(r"[.!\n]", bruto.strip(), maxsplit=1)[0] if bruto.strip() else ""
    return {
        "tam_legenda": len(bruto),
        "pergunta": "?" in primeira[:200],
        "hashtags": len(re.findall(r"#\w+", bruto)),
        "mencoes": len(re.findall(r"@[\w.]+", bruto)),
    }


# --- Login ------------------------------------------------------------------

def _auth_configurado():
    try:
        return "auth" in st.secrets
    except Exception:
        return False


def _gate_de_login():
    if not _auth_configurado():
        return
    if not st.user.is_logged_in:
        st.title("Painel do Instagram")
        st.caption("Instituto Cerrados")
        st.info("Painel interno. Entre com sua conta Google institucional.")
        st.button("Entrar com Google", on_click=st.login, type="primary")
        st.stop()
    email = (st.user.email or "").strip().lower()
    if email not in EMAILS_AUTORIZADOS:
        st.error(f"Este painel é restrito. A conta {st.user.email} não tem acesso.")
        st.button("Sair", on_click=st.logout)
        st.stop()
    st.sidebar.caption(f"Logado como {st.user.email}")
    st.sidebar.button("Sair", on_click=st.logout)


def _token():
    try:
        if "META_TOKEN" in st.secrets:
            return st.secrets["META_TOKEN"]
    except Exception:
        pass
    if os.environ.get("META_TOKEN"):
        return os.environ["META_TOKEN"]
    if ENV_LOCAL.exists():
        for linha in ENV_LOCAL.read_text(encoding="utf-8").splitlines():
            if linha.startswith("META_TOKEN="):
                return linha.split("=", 1)[1].strip().strip('"')
    return None


# --- API da Meta --------------------------------------------------------------

class ErroMeta(Exception):
    pass


def _get(caminho, token, **params):
    params["access_token"] = token
    url = caminho if caminho.startswith("http") else f"{API}/{caminho}"
    try:
        r = requests.get(url, params=None if caminho.startswith("http") else params, timeout=60)
    except requests.RequestException as e:
        raise ErroMeta(f"Falha de rede ao consultar a Meta ({type(e).__name__}).") from None
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise ErroMeta(f"A Meta respondeu {r.status_code}: {msg.replace(token, '***')}")
    return r.json()


def _ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _janelas(inicio, fim, passo=30):
    """As consultas de conta aceitam no máximo 30 dias por vez."""
    a = inicio
    while a < fim:
        b = min(a + timedelta(days=passo), fim)
        yield a, b
        a = b


def _totais_janela(token, a, b):
    tot = {}
    for d in _get(f"{IG_USER_ID}/insights", token, metric=METRICAS_CONTA, period="day",
                  metric_type="total_value", since=_ts(a), until=_ts(b))["data"]:
        tot[d["name"]] = d.get("total_value", {}).get("value", 0)
    try:
        dados = _get(f"{IG_USER_ID}/insights", token, metric="follows_and_unfollows", period="day",
                     metric_type="total_value", breakdown="follow_type", since=_ts(a), until=_ts(b))["data"]
        quebras = dados[0].get("total_value", {}).get("breakdowns", []) if dados else []
        for res in (quebras[0].get("results", []) if quebras else []):
            chave = "novos" if res["dimension_values"][0] == "FOLLOWER" else "deixaram"
            tot[chave] = res["value"]
    except ErroMeta:
        pass
    tot.setdefault("novos", 0)
    tot.setdefault("deixaram", 0)
    return tot


def _somar(partes):
    saida = {}
    for p in partes:
        for k, v in p.items():
            saida[k] = saida.get(k, 0) + (v or 0)
    return saida


def _serie(token, metrica, inicio, fim):
    pontos = {}
    for a, b in _janelas(inicio, fim):
        dados = _get(f"{IG_USER_ID}/insights", token, metric=metrica, period="day",
                     since=_ts(a), until=_ts(b))["data"]
        for v in (dados[0]["values"] if dados else []):
            dia = datetime.strptime(v["end_time"][:10], "%Y-%m-%d").date() - timedelta(days=1)
            pontos[dia.isoformat()] = v["value"]
    return sorted(pontos.items())


def _formato(p):
    if p.get("media_product_type") == "REELS":
        return "Reels"
    return {"CAROUSEL_ALBUM": "Carrossel", "IMAGE": "Foto", "VIDEO": "Vídeo"}.get(p.get("media_type"), "Outro")


def _insights_post(token, p):
    metricas = ["reach", "views", "saved", "shares", "total_interactions"]
    if p.get("media_product_type") == "REELS":
        metricas.append("ig_reels_avg_watch_time")
    try:
        dados = _get(f"{p['id']}/insights", token, metric=",".join(metricas))["data"]
        return {d["name"]: d["values"][0]["value"] for d in dados}
    except ErroMeta:
        saida = {}
        for m in metricas:
            try:
                saida[m] = _get(f"{p['id']}/insights", token, metric=m)["data"][0]["values"][0]["value"]
            except ErroMeta:
                saida[m] = None
        return saida


def _posts(token, desde):
    campos = ("id,caption,timestamp,media_type,media_product_type,permalink,"
              "media_url,thumbnail_url,like_count,comments_count,children{id}")
    posts, pagina = [], _get(f"{IG_USER_ID}/media", token, fields=campos, limit=50)
    while True:
        fim = False
        for p in pagina.get("data", []):
            if datetime.strptime(p["timestamp"][:10], "%Y-%m-%d").date() < desde:
                fim = True
                break
            posts.append(p)
        prox = pagina.get("paging", {}).get("next")
        if fim or not prox:
            return posts
        pagina = _get(prox, token)


def _publico(token):
    def quebra(breakdown):
        try:
            dados = _get(f"{IG_USER_ID}/insights", token, metric="follower_demographics", period="lifetime",
                         metric_type="total_value", breakdown=breakdown, timeframe="this_month")["data"]
            res = dados[0]["total_value"]["breakdowns"][0]["results"]
            return [[r["dimension_values"], r["value"]] for r in res]
        except (ErroMeta, KeyError, IndexError):
            return []
    cidades = sorted(([d[0], v] for d, v in quebra("city")), key=lambda x: -x[1])
    paises = sorted(([d[0], v] for d, v in quebra("country")), key=lambda x: -x[1])
    idade = [[d[0], d[1], v] for d, v in quebra("age,gender")]
    return {"cidades": cidades[:12], "paises": paises[:10], "idade_genero": idade}


def _uma_referencia(token, usuario):
    campos = (f"business_discovery.username({usuario}){{username,name,followers_count,media_count,profile_picture_url,"
              f"media.limit({POSTS_POR_REFERENCIA}){{caption,like_count,comments_count,media_type,media_product_type,"
              f"permalink,timestamp,media_url,thumbnail_url}}}}")
    b = _get(IG_USER_ID, token, fields=campos)["business_discovery"]
    seg = b.get("followers_count") or 0
    posts = []
    for p in b.get("media", {}).get("data", []):
        tema, temas, tipo, tipos = classificar(p.get("caption"))
        curt, com = p.get("like_count"), p.get("comments_count") or 0
        quando = datetime.strptime(p["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(BRASILIA)
        posts.append({
            "data": quando.strftime("%Y-%m-%dT%H:%M"),
            "formato": _formato(p),
            "legenda": " ".join((p.get("caption") or "").split())[:300],
            "imagem": p.get("thumbnail_url") or p.get("media_url"),
            "link": p.get("permalink"),
            "curtidas": curt,
            "comentarios": com,
            "taxa": ((curt or 0) + com) / seg if seg and curt is not None else None,
            "tema": tema,
            "temas": temas,
            "tipo": tipo,
            "tipos": tipos,
        })
    return {"usuario": b.get("username", usuario), "nome": b.get("name"), "seguidores": seg,
            "total_posts": b.get("media_count"), "foto": b.get("profile_picture_url"), "posts": posts}


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def carregar_referencias(token, usuarios):
    with ThreadPoolExecutor(max_workers=5) as ex:
        futuros = {u: ex.submit(_uma_referencia, token, u) for u in usuarios}
    saida, falhas = [], []
    for u, f in futuros.items():
        try:
            saida.append(f.result())
        except ErroMeta:
            falhas.append(u)
    return {"perfis": saida, "falhas": falhas}


@st.cache_data(ttl=3 * 3600, show_spinner=False)
def carregar(token):
    hoje = date.today()
    perfil = _get(IG_USER_ID, token, fields="username,name,followers_count,follows_count,media_count,profile_picture_url")
    posts_brutos = _posts(token, hoje - timedelta(days=DIAS_HISTORICO))

    with ThreadPoolExecutor(max_workers=8) as ex:
        jobs = {}
        for p in PERIODOS:
            for nome, (a, b) in {"atual": (hoje - timedelta(days=p), hoje),
                                 "anterior": (hoje - timedelta(days=2 * p), hoje - timedelta(days=p))}.items():
                jobs[(p, nome)] = [ex.submit(_totais_janela, token, x, y) for x, y in _janelas(a, b)]
        f_alcance = ex.submit(_serie, token, "reach", hoje - timedelta(days=DIAS_HISTORICO), hoje)
        f_seg = ex.submit(_serie, token, "follower_count", hoje - timedelta(days=29), hoje)
        f_pub = ex.submit(_publico, token)
        f_ins = [ex.submit(_insights_post, token, p) for p in posts_brutos]

        periodos = {}
        for (p, nome), fs in jobs.items():
            periodos.setdefault(str(p), {})[nome] = _somar(f.result() for f in fs)
        alcance = f_alcance.result()
        try:
            seguidores = f_seg.result()
        except ErroMeta:
            seguidores = []
        publico = f_pub.result()
        insights = [f.result() for f in f_ins]

    posts = []
    for p, ins in zip(posts_brutos, insights):
        quando = datetime.strptime(p["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(BRASILIA)
        alc, inter = ins.get("reach"), ins.get("total_interactions")
        reels_ms = ins.get("ig_reels_avg_watch_time")
        tema, temas, tipo, tipos = classificar(p.get("caption"))
        attr = atributos(p.get("caption"))
        n_fotos = len(p.get("children", {}).get("data", [])) if p.get("media_type") == "CAROUSEL_ALBUM" else None
        posts.append({
            "tema": tema,
            "temas": temas,
            "tipo": tipo,
            "tipos": tipos,
            "n_fotos": n_fotos,
            **attr,
            "id": p["id"],
            "data": quando.strftime("%Y-%m-%dT%H:%M"),
            "formato": _formato(p),
            "legenda": " ".join((p.get("caption") or "").split())[:300],
            "imagem": p.get("thumbnail_url") or p.get("media_url"),
            "link": p.get("permalink"),
            "alcance": alc,
            "visualizacoes": ins.get("views"),
            "curtidas": p.get("like_count"),
            "comentarios": p.get("comments_count"),
            "salvamentos": ins.get("saved"),
            "compartilhamentos": ins.get("shares"),
            "interacoes": inter,
            "engajamento": (inter / alc) if alc and inter is not None else None,
            "tempo_reels": round(reels_ms / 1000, 1) if reels_ms else None,
        })

    return {
        "perfil": {
            "usuario": perfil.get("username"),
            "nome": perfil.get("name"),
            "seguidores": perfil.get("followers_count"),
            "seguindo": perfil.get("follows_count"),
            "posts": perfil.get("media_count"),
            "foto": perfil.get("profile_picture_url"),
        },
        "gerado_em": datetime.now(BRASILIA).strftime("%d/%m/%Y %H:%M"),
        "hoje": hoje.isoformat(),
        "periodos": periodos,
        "serie_alcance": alcance,
        "serie_seguidores": seguidores,
        "posts": posts,
        "publico": publico,
    }


# --- Interface ----------------------------------------------------------------

ESQUELETO = """
<style>
  .esq-wrap{font-family:system-ui,sans-serif;padding:8px 4px}
  .esq{background:linear-gradient(90deg,#eee 0%,#f7f7f7 40%,#eee 80%);background-size:200% 100%;
       animation:brilho 1.2s ease-in-out infinite;border-radius:16px}
  @keyframes brilho{0%{background-position:100% 0}100%{background-position:-100% 0}}
  .esq-grade{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:18px}
  .esq-faixa{height:6px;border-radius:6px;background:linear-gradient(90deg,#FEDA75,#FA7E1E,#D62976,#962FBF,#4F5BD5)}
  .esq-txt{color:#888;font-size:14px;margin-top:14px}
</style>
<div class="esq-wrap">
  <div class="esq-faixa"></div>
  <div class="esq" style="height:56px;margin-top:16px"></div>
  <div class="esq-grade">
    <div class="esq" style="height:110px"></div><div class="esq" style="height:110px"></div>
    <div class="esq" style="height:110px"></div><div class="esq" style="height:110px"></div>
  </div>
  <div class="esq" style="height:280px;margin-top:18px"></div>
  <div class="esq-txt">Buscando as métricas do @institutocerrados na Meta… (depois de algumas horas sem acesso, a primeira carga pode levar até 1 minuto)</div>
</div>
"""

AJUSTE_ALTURA = """
<script>
(function () {
  function ajustar() {
    try {
      var alvo = window.frameElement;
      if (!alvo || !window.parent) return;
      var h = window.parent.innerHeight;
      if (h) alvo.style.height = Math.max(h - 4, 480) + 'px';
    } catch (e) {}
  }
  window.addEventListener('load', ajustar);
  window.addEventListener('resize', ajustar);
  try { window.parent.addEventListener('resize', ajustar); } catch (e) {}
  ajustar(); setTimeout(ajustar, 300);
})();
</script>
"""


def main():
    _gate_de_login()
    st.markdown(
        """
        <style>
          header[data-testid="stHeader"], footer, #MainMenu { visibility: hidden; height: 0; }
          .block-container { padding: 0 !important; max-width: 100% !important; }
          iframe { border: none; display: block; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    token = _token()
    if not token:
        st.error("Token da Meta não configurado. Adicione META_TOKEN nos secrets do app.")
        st.stop()

    lugar = st.empty()
    lugar.markdown(ESQUELETO, unsafe_allow_html=True)
    # As duas buscas são independentes: rodam em paralelo para encurtar a
    # primeira carga depois que o cache expira.
    ctx = get_script_run_ctx()
    with ThreadPoolExecutor(max_workers=2) as ex:
        def com_contexto(fn, *args):
            add_script_run_ctx(threading.current_thread(), ctx)
            return fn(*args)
        f_dados = ex.submit(com_contexto, carregar, token)
        f_refs = ex.submit(com_contexto, carregar_referencias, token, tuple(REFERENCIAS))
        try:
            dados = f_dados.result()
        except ErroMeta as e:
            lugar.error(f"Não foi possível ler os dados da Meta. {e}")
            st.stop()
        try:
            refs = f_refs.result()
        except ErroMeta:
            refs = {"perfis": [], "falhas": list(REFERENCIAS)}
    dados = dict(dados, referencias=refs, temas_lista=list(TEMAS) + ["Outros"], tipos_lista=list(TIPOS) + ["Outros"])

    modelo = (BASE / "painel.html").read_text(encoding="utf-8")
    embutido = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    html = modelo.replace("/*__DADOS__*/null", embutido).replace("</body>", AJUSTE_ALTURA + "</body>")
    with lugar.container():
        st.components.v1.html(html, height=900, scrolling=True)


main()
