# Painel do Instagram - Instituto Cerrados

Painel dinamico das metricas do Instagram @institutocerrados (alcance, visualizacoes,
interacoes, desempenho por formato, ranking de posts e melhores dias/horarios).

- Dados ao vivo da API oficial da Meta (Instagram Graph API), com cache de 3 horas.
- Token: usuario do sistema "coletametricasic" do app Meta "metricas_IC_ins"
  (portfolio Instituto Cerrados), sem expiracao. Fica em `META_TOKEN` nos Secrets.
- Acesso restrito por login Google aos e-mails de `EMAILS_AUTORIZADOS` (`app.py`).
- Complementa a planilha semanal "Painel Instagram IC.xlsx" no Drive
  (Executivo/Claude/Comunicacao_executiva), gerada pelo coleta_metricas.py.

## Rodar localmente

```
pip install -r requirements.txt
streamlit run app.py
```

Sem o bloco `[auth]` nos secrets o app roda sem login (dev local).
