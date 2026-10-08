# Desktop sandbox: Xvfb virtual screen (+ optional Openbox) + Chromium with CDP on 127.0.0.1:9222.
# The IntraLLM agent drives it with `desktopctl` through two tool families:
#   computer  - screenshots, mouse and keyboard via the X XTEST extension (computer use)
#   browser   - Playwright connected over CDP to the same visible Chromium
#
# Build:  docker build -t intrallm/sandbox-desktop:latest sandbox-desktop/
# Based on Microsoft's official Playwright image, which already ships Chromium,
# its system dependencies and Xvfb.
ARG BASE=mcr.microsoft.com/playwright/python:v1.56.0-noble
FROM ${BASE}

# Window manager, terminal and CJK fonts. Needs apt access; set to 0 for
# air-gapped builds (Chromium works without a WM, Chinese text renders as boxes).
ARG DESKTOP_EXTRAS=1
RUN if [ "$DESKTOP_EXTRAS" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends openbox xterm fonts-noto-cjk fonts-noto-color-emoji \
      && rm -rf /var/lib/apt/lists/*; \
    fi

RUN pip install --no-cache-dir playwright==1.56.0 python-xlib==0.33 pillow==11.3.0 \
 && ln -sf "$(ls -d /ms-playwright/chromium-*/chrome-linux/chrome | head -1)" /usr/local/bin/chromium

COPY bin/ /opt/intrallm/bin/
COPY openbox-rc.xml /etc/xdg/openbox/rc.xml
RUN chmod +x /opt/intrallm/bin/* \
 && ln -sf /opt/intrallm/bin/desktopctl /usr/local/bin/desktopctl \
 && useradd --create-home --shell /bin/bash sandbox \
 && mkdir -p /workspace && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace
ENV HOME=/home/sandbox DISPLAY=:1 SCREEN_WIDTH=1280 SCREEN_HEIGHT=800 PYTHONUNBUFFERED=1
CMD ["/opt/intrallm/bin/start-desktop"]
