"""The pipeline stages (fundamentals-first; IV-rank dropped for v1).

1. universe          — cheap price/market-cap/exchange universe (FMP screener)
2. rate_fundamentals — bulk pre-rank, deep-rate the top N (pythonBot criteria),
                       apply the earnings blackout; keep the best names
3. pull_chains       — fetch 30-45 DTE put chains for survivors (Schwab)
4. select_strike     — pick the put nearest -0.20 delta per expiry; compute yield
5. rank              — order candidates by annualized yield (IV shown as a column)
"""
