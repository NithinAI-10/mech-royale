# mechfight/1 bot "survivor". Standard library only; nav.bin is a grid precomputed from the static map.
FROM python:3.13-slim
WORKDIR /bot
COPY mechfight.py nav.py bot.py nav.bin /bot/
RUN python3 -m compileall -q /bot
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
