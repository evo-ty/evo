FROM node:22-bookworm-slim
ENV NODE_ENV=production
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser
COPY evo-entrypoint /usr/local/bin/evo-entrypoint
RUN chmod 0555 /usr/local/bin/evo-entrypoint
WORKDIR /workspace
USER appuser
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/evo-entrypoint"]
CMD ["node"]
