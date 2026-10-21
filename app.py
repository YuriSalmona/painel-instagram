# -*- coding: utf-8 -*-
"""Painel do Instagram — Instituto Cerrados (acesso restrito).

Lê as métricas ao vivo na API da Meta (Instagram Graph API) com o token do
usuário do sistema "coletametricasic" (app metricas_IC_ins, portfólio
Instituto Cerrados). O token fica em st.secrets["META_TOKEN"].

Login Google restrito aos e-mails de EMAILS_AUTORIZADOS, exigido quando o
bloco [auth] existe nos secrets (produção). Em dev local, sem esse bloco,
roda sem login.
"""

import os
import pathlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import altair as alt
import pandas as pd
import requests
import streamlit as st

st.set_page_config(
    page_title="Painel do Instagram — Instituto Cerrados",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Quem pode abrir o painel. Para liberar para mais alguém do IC, acrescente
# o e-mail aqui e publique.
EMAILS_AUTORIZADOS = {"yuri@cerrados.org", "rebeca@cerrados.org"}

IG_USER_ID = "17841401645503865"
API = "https://graph.facebook.com/v21.0"
ENV_LOCAL = pathlib.Path(r"C:\Users\yuri\.claude\projects\C--comunicacao_IC\metricas_instagram\.env")

VERDE, BEGE, LARANJA = "#004F23", "#DDCCA4", "#E85718"
TONS = [VERDE, "#6B8F5E", BEGE, "#2F3E2A"]
DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


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
    try:
        r = requests.get(f"{API}/{caminho}", params=params, timeout=60)
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
    """A API só aceita até 30 dias por consulta de série diária."""
    a = inicio
    while a < fim:
        b = min(a + timedelta(days=passo), fim)
        yield a, b
        a = b


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


@st.cache_data(ttl=3 * 3600, show_spinner=False)
def carregar(token, dias):
    hoje = date.today()
    inicio = hoje - timedelta(days=dias)
    ant_inicio = inicio - timedelta(days=dias)

    perfil = _get(IG_USER_ID, token, fields="username,name,followers_count,follows_count,media_count")

    def periodo(a, b):
        resultado = {}
        for x, y in _janelas(a, b):
            dados = _get(f"{IG_USER_ID}/insights", token,
                         metric="reach,views,total_interactions,accounts_engaged",
                         period="day", metric_type="total_value", since=_ts(x), until=_ts(y))["data"]
            for d in dados:
                resultado[d["name"]] = resultado.get(d["name"], 0) + d["total_value"]["value"]
        return resultado

    def serie(metrica, a, b):
        pontos = []
        for x, y in _janelas(a, b):
            dados = _get(f"{IG_USER_ID}/insights", token, metric=metrica, period="day",
                         since=_ts(x), until=_ts(y))["data"]
            for v in (dados[0]["values"] if dados else []):
                dia = datetime.strptime(v["end_time"][:10], "%Y-%m-%d").date() - timedelta(days=1)
                pontos.append({"data": pd.Timestamp(dia), metrica: v["value"]})
        return pd.DataFrame(pontos)

    campos = ("id,caption,timestamp,media_type,media_product_type,permalink,"
              "media_url,thumbnail_url,like_count,comments_count")
    posts, pagina = [], _get(f"{IG_USER_ID}/media", token, fields=campos, limit=50)
    while True:
        fim = False
        for p in pagina.get("data", []):
            if datetime.strptime(p["timestamp"][:10], "%Y-%m-%d").date() < inicio:
                fim = True
                break
            posts.append(p)
        prox = pagina.get("paging", {}).get("next")
        if fim or not prox:
            break
        r = requests.get(prox, timeout=60)
        if r.status_code != 200:
            break
        pagina = r.json()

    with ThreadPoolExecutor(max_workers=8) as ex:
        f_atual = ex.submit(periodo, inicio, hoje)
        f_ant = ex.submit(periodo, ant_inicio, inicio)
        f_alcance = ex.submit(serie, "reach", inicio, hoje)
        f_seg = ex.submit(serie, "follower_count", max(inicio, hoje - timedelta(days=29)), hoje)
        f_ins = [ex.submit(_insights_post, token, p) for p in posts]
        atual, anterior = f_atual.result(), f_ant.result()
        alcance = f_alcance.result()
        try:
            seguidores = f_seg.result()
        except ErroMeta:
            seguidores = pd.DataFrame(columns=["data", "follower_count"])
        insights = [f.result() for f in f_ins]

    linhas = []
    for p, ins in zip(posts, insights):
        ts = datetime.strptime(p["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(timezone(timedelta(hours=-3)))
        alc, inter = ins.get("reach"), ins.get("total_interactions")
        linhas.append({
            "Miniatura": p.get("thumbnail_url") or p.get("media_url"),
            "Data": pd.Timestamp(ts.replace(tzinfo=None)),
            "Formato": _formato(p),
            "Legenda": " ".join((p.get("caption") or "").split())[:140],
            "Alcance": alc,
            "Visualizações": ins.get("views"),
            "Curtidas": p.get("like_count"),
            "Comentários": p.get("comments_count"),
            "Salvamentos": ins.get("saved"),
            "Compartilhamentos": ins.get("shares"),
            "Interações": inter,
            "Engajamento": (inter / alc) if alc and inter is not None else None,
            "Tempo médio Reels (s)": round(ins["ig_reels_avg_watch_time"] / 1000, 1) if ins.get("ig_reels_avg_watch_time") else None,
            "Link": p.get("permalink"),
        })
    df_posts = pd.DataFrame(linhas)
    return {
        "perfil": perfil, "atual": atual, "anterior": anterior,
        "alcance": alcance, "seguidores": seguidores, "posts": df_posts,
        "gerado_em": datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y %H:%M"),
    }


# --- Interface ----------------------------------------------------------------

CSS = f"""
<link href="https://fonts.googleapis.com/css2?family=Londrina+Solid:wght@400&family=Montserrat:wght@400;700&display=swap" rel="stylesheet">
<style>
  html, body, [class*="css"], .stMarkdown, .stDataFrame, p, li, label {{ font-family: 'Montserrat', sans-serif; }}
  h1, h2, h3 {{ font-family: 'Londrina Solid', cursive !important; color: {VERDE} !important; font-weight: 400 !important; }}
  [data-testid="stMetric"] {{ background: {BEGE}; border-radius: 10px; padding: 14px 16px; }}
  [data-testid="stMetricLabel"] p {{ color: {VERDE}; font-weight: 700; }}
  [data-testid="stMetricValue"] {{ font-family: 'Londrina Solid', cursive; color: {VERDE}; }}
  .rodape {{ font-size: 0.75rem; color: #555; margin-top: 2rem; }}
  .acento {{ color: {LARANJA}; font-weight: 700; }}
</style>
"""


def _delta(atual, anterior):
    if not anterior or atual is None:
        return None
    return f"{(atual - anterior) / anterior:+.0%} vs. período anterior"


def _fmt(n):
    return "—" if n is None or pd.isna(n) else f"{n:,.0f}".replace(",", ".")


def _grafico_linha(df, y, titulo):
    return (alt.Chart(df, title=titulo).mark_line(color=VERDE, strokeWidth=2.5, point=alt.OverlayMarkDef(color=VERDE, size=18))
            .encode(x=alt.X("data:T", title=None, axis=alt.Axis(format="%d/%m")),
                    y=alt.Y(f"{y}:Q", title=None),
                    tooltip=[alt.Tooltip("data:T", title="Dia", format="%d/%m/%Y"), alt.Tooltip(f"{y}:Q", title=titulo, format=",.0f")])
            .properties(height=260))


def main():
    _gate_de_login()
    st.markdown(CSS, unsafe_allow_html=True)

    token = _token()
    if not token:
        st.error("Token da Meta não configurado. Adicione META_TOKEN nos secrets do app.")
        st.stop()

    with st.sidebar:
        st.markdown("### Filtros")
        dias = st.radio("Período", [30, 90, 180], index=1, format_func=lambda d: f"Últimos {d} dias", horizontal=False)
        if st.button("Atualizar dados agora"):
            carregar.clear()
        st.caption("Os dados vêm direto da Meta e ficam guardados por até 3 horas.")

    with st.spinner("Buscando as métricas na Meta…"):
        try:
            d = carregar(token, dias)
        except ErroMeta as e:
            st.error(f"Não foi possível ler os dados da Meta. {e}")
            st.stop()

    perfil, atual, anterior, posts = d["perfil"], d["atual"], d["anterior"], d["posts"]

    st.title("Painel do Instagram — Instituto Cerrados")
    st.caption(f"@{perfil['username']} · dados de {d['gerado_em']} · últimos {dias} dias comparados aos {dias} dias anteriores")

    formatos = sorted(posts["Formato"].unique()) if not posts.empty else []
    with st.sidebar:
        escolha = st.multiselect("Formatos", formatos, default=formatos)
    p = posts[posts["Formato"].isin(escolha)] if not posts.empty else posts

    # Indicadores
    eng = p["Engajamento"].dropna().mean() if not p.empty else None
    c = st.columns(3) + st.columns(3)
    c[0].metric("Seguidores", _fmt(perfil["followers_count"]))
    c[1].metric("Alcance", _fmt(atual.get("reach")), _delta(atual.get("reach"), anterior.get("reach")),
                help="Contas únicas que viram algum conteúdo no período." if dias <= 30 else
                "Soma do alcance de cada janela de 30 dias: a Meta só conta pessoas únicas em até 30 dias, "
                "então quem voltou em meses diferentes conta mais de uma vez.")
    c[2].metric("Visualizações", _fmt(atual.get("views")), _delta(atual.get("views"), anterior.get("views")),
                help="Quantas vezes o conteúdo foi exibido, contando repetições.")
    c[3].metric("Interações", _fmt(atual.get("total_interactions")),
                _delta(atual.get("total_interactions"), anterior.get("total_interactions")),
                help="Curtidas, comentários, salvamentos e compartilhamentos.")
    c[4].metric("Posts publicados", _fmt(len(p)))
    c[5].metric("Engajamento médio", "—" if eng is None or pd.isna(eng) else f"{eng:.1%}".replace(".", ","),
                help="Interações ÷ alcance, na média dos posts do período.")

    aba1, aba2, aba3, aba4 = st.tabs(["Visão geral", "Posts", "Quando publicar", "Como ler"])

    with aba1:
        g1, g2 = st.columns(2)
        if not d["alcance"].empty:
            g1.altair_chart(_grafico_linha(d["alcance"], "reach", "Alcance diário da conta"), use_container_width=True)
        seg = d["seguidores"]
        if not seg.empty:
            g2.altair_chart(
                alt.Chart(seg, title="Novos seguidores por dia (últimos 30 dias)").mark_bar(color=VERDE)
                .encode(x=alt.X("data:T", title=None, axis=alt.Axis(format="%d/%m")),
                        y=alt.Y("follower_count:Q", title=None),
                        tooltip=[alt.Tooltip("data:T", title="Dia", format="%d/%m/%Y"),
                                 alt.Tooltip("follower_count:Q", title="Novos seguidores")])
                .properties(height=260), use_container_width=True)

        st.subheader("Desempenho por formato")
        if p.empty:
            st.info("Nenhum post no período.")
        else:
            por_formato = (p.groupby("Formato")
                           .agg(Posts=("Link", "count"), Alcance=("Alcance", "mean"), Interações=("Interações", "mean"),
                                Salvamentos=("Salvamentos", "mean"), Compartilhamentos=("Compartilhamentos", "mean"),
                                Engajamento=("Engajamento", "mean"))
                           .reset_index().sort_values("Alcance", ascending=False))
            f1, f2 = st.columns([1, 1])
            f1.altair_chart(
                alt.Chart(por_formato, title="Alcance médio por post").mark_bar(color=VERDE)
                .encode(x=alt.X("Alcance:Q", title=None), y=alt.Y("Formato:N", sort="-x", title=None),
                        tooltip=["Formato", alt.Tooltip("Alcance:Q", format=",.0f"), "Posts"])
                .properties(height=200), use_container_width=True)
            f2.dataframe(por_formato, hide_index=True, use_container_width=True, column_config={
                "Alcance": st.column_config.NumberColumn("Alcance médio", format="%.0f"),
                "Interações": st.column_config.NumberColumn("Interações médias", format="%.0f"),
                "Salvamentos": st.column_config.NumberColumn("Salvam. médios", format="%.1f"),
                "Compartilhamentos": st.column_config.NumberColumn("Compart. médios", format="%.1f"),
                "Engajamento": st.column_config.NumberColumn(format="percent"),
            })

            st.subheader("Alcance de cada post")
            serie_posts = p.sort_values("Data").reset_index(drop=True)
            serie_posts["Post"] = [f"{d:%d/%m} · {i + 1}" for i, d in enumerate(serie_posts["Data"])]
            st.altair_chart(
                alt.Chart(serie_posts).mark_bar()
                .encode(x=alt.X("Post:N", sort=None, title=None, axis=alt.Axis(labelAngle=-45)),
                        y=alt.Y("Alcance:Q", title=None),
                        color=alt.Color("Formato:N", scale=alt.Scale(range=TONS), legend=alt.Legend(orient="top", title=None)),
                        tooltip=[alt.Tooltip("Data:T", format="%d/%m/%Y"), "Formato", "Legenda",
                                 alt.Tooltip("Alcance:Q", format=",.0f"), alt.Tooltip("Engajamento:Q", format=".1%")])
                .properties(height=280), use_container_width=True)

    with aba2:
        if p.empty:
            st.info("Nenhum post no período.")
        else:
            ordem = st.selectbox("Ordenar por", ["Alcance", "Engajamento", "Compartilhamentos", "Salvamentos", "Data"])
            tabela = p.sort_values(ordem, ascending=False, na_position="last")
            st.dataframe(tabela, hide_index=True, use_container_width=True, height=560, column_config={
                "Miniatura": st.column_config.ImageColumn(width="small"),
                "Data": st.column_config.DatetimeColumn(format="DD/MM/YY HH:mm"),
                "Legenda": st.column_config.TextColumn(width="large"),
                "Engajamento": st.column_config.NumberColumn(format="percent"),
                "Link": st.column_config.LinkColumn(display_text="abrir"),
            })
            st.download_button("Baixar tabela (CSV)", tabela.drop(columns=["Miniatura"]).to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"posts_instagram_ic_{date.today():%Y%m%d}.csv", mime="text/csv")

    with aba3:
        if len(p) < 3:
            st.info("Poucos posts no período para comparar dias e horários. Amplie o período no filtro ao lado.")
        else:
            q = p.dropna(subset=["Alcance"]).copy()
            q["Dia"] = q["Data"].dt.dayofweek.map(dict(enumerate(DIAS_SEMANA)))
            q["Faixa"] = pd.cut(q["Data"].dt.hour, [0, 9, 12, 15, 18, 21, 24], right=False,
                                labels=["até 9h", "9h–12h", "12h–15h", "15h–18h", "18h–21h", "21h+"])
            h1, h2 = st.columns(2)
            por_dia = q.groupby("Dia").agg(Alcance=("Alcance", "mean"), Posts=("Link", "count")).reindex(DIAS_SEMANA).dropna().reset_index()
            h1.altair_chart(
                alt.Chart(por_dia, title="Alcance médio por dia da semana").mark_bar(color=VERDE)
                .encode(x=alt.X("Dia:N", sort=DIAS_SEMANA, title=None), y=alt.Y("Alcance:Q", title=None),
                        tooltip=["Dia", alt.Tooltip("Alcance:Q", format=",.0f"), "Posts"])
                .properties(height=260), use_container_width=True)
            por_hora = q.groupby("Faixa", observed=True).agg(Alcance=("Alcance", "mean"), Posts=("Link", "count")).reset_index()
            h2.altair_chart(
                alt.Chart(por_hora, title="Alcance médio por horário de publicação").mark_bar(color=VERDE)
                .encode(x=alt.X("Faixa:N", sort=None, title=None), y=alt.Y("Alcance:Q", title=None),
                        tooltip=["Faixa", alt.Tooltip("Alcance:Q", format=",.0f"), "Posts"])
                .properties(height=260), use_container_width=True)
            st.caption(f"Base: {len(q)} posts. Com poucos posts por dia ou horário, um único post forte puxa a média — "
                       "use como pista para testar, não como regra.")

    with aba4:
        st.markdown(f"""
**Alcance** — contas únicas que viram o conteúdo. Uma pessoa que viu três vezes conta uma.

**Visualizações** — quantas vezes o conteúdo foi exibido, contando repetições.

**Interações** — soma de curtidas, comentários, salvamentos e compartilhamentos.

**Engajamento** — interações ÷ alcance. Mostra que fração de quem viu reagiu ao post.

**Salvamentos** — sinal de conteúdo útil: a pessoa quer voltar a ele.

**Compartilhamentos** — o que faz um post <span class="acento">ganhar alcance fora dos seguidores</span>.

**Tempo médio Reels** — segundos assistidos, em média, por visualização.

**Comparação** — a seta em cada indicador compara o período escolhido com o período imediatamente anterior, de mesmo tamanho.

**Posts recentes** — nas primeiras 48 horas o post ainda acumula alcance; para comparar, prefira posts com mais de 7 dias.

**Fonte** — API oficial da Meta, conta @institutocerrados. Stories não entram (a Meta só guarda por 24 horas).
""", unsafe_allow_html=True)

    st.markdown('<div class="rodape">Instituto Cerrados · Comunicação</div>', unsafe_allow_html=True)


main()
