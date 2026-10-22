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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests
import streamlit as st

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
              "media_url,thumbnail_url,like_count,comments_count")
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
        posts.append({
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
  <div class="esq-txt">Buscando as métricas do @institutocerrados na Meta… (a primeira carga leva uns 30 segundos)</div>
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
    try:
        dados = carregar(token)
    except ErroMeta as e:
        lugar.error(f"Não foi possível ler os dados da Meta. {e}")
        st.stop()

    modelo = (BASE / "painel.html").read_text(encoding="utf-8")
    embutido = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    html = modelo.replace("/*__DADOS__*/null", embutido).replace("</body>", AJUSTE_ALTURA + "</body>")
    with lugar.container():
        st.components.v1.html(html, height=900, scrolling=True)


main()
