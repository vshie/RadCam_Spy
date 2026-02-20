FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask websockets

WORKDIR /app
COPY app/ .

RUN mkdir -p /app/data/logs

# Bundle all frontend vendor dependencies so the UI works fully offline.
RUN mkdir -p /app/static/vendor/mdi/css /app/static/vendor/mdi/fonts /app/static/vendor/fonts \
    && curl -sL -o /app/static/vendor/vue.global.prod.js \
       "https://cdn.jsdelivr.net/npm/vue@3.3.11/dist/vue.global.prod.js" \
    && curl -sL -o /app/static/vendor/vuetify.min.js \
       "https://cdn.jsdelivr.net/npm/vuetify@3.4.6/dist/vuetify.min.js" \
    && curl -sL -o /app/static/vendor/vuetify.min.css \
       "https://cdn.jsdelivr.net/npm/vuetify@3.4.6/dist/vuetify.min.css" \
    && curl -sL -o /app/static/vendor/chart.umd.min.js \
       "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" \
    && curl -sL -o /app/static/vendor/mdi/css/materialdesignicons.min.css \
       "https://cdn.jsdelivr.net/npm/@mdi/font@7.3.67/css/materialdesignicons.min.css" \
    && curl -sL -o /app/static/vendor/mdi/fonts/materialdesignicons-webfont.woff2 \
       "https://cdn.jsdelivr.net/npm/@mdi/font@7.3.67/fonts/materialdesignicons-webfont.woff2" \
    && curl -sL -o /app/static/vendor/mdi/fonts/materialdesignicons-webfont.woff \
       "https://cdn.jsdelivr.net/npm/@mdi/font@7.3.67/fonts/materialdesignicons-webfont.woff" \
    && curl -sL -o /app/static/vendor/mdi/fonts/materialdesignicons-webfont.eot \
       "https://cdn.jsdelivr.net/npm/@mdi/font@7.3.67/fonts/materialdesignicons-webfont.eot" \
    && curl -sL -o /app/static/vendor/mdi/fonts/materialdesignicons-webfont.ttf \
       "https://cdn.jsdelivr.net/npm/@mdi/font@7.3.67/fonts/materialdesignicons-webfont.ttf" \
    && curl -sL -o /app/static/vendor/fonts/Inter-Regular.woff2 \
       "https://cdn.jsdelivr.net/gh/rsms/inter@4.0/web/font-files/Inter-Regular.woff2" \
    && curl -sL -o /app/static/vendor/fonts/Inter-Medium.woff2 \
       "https://cdn.jsdelivr.net/gh/rsms/inter@4.0/web/font-files/Inter-Medium.woff2" \
    && curl -sL -o /app/static/vendor/fonts/Inter-SemiBold.woff2 \
       "https://cdn.jsdelivr.net/gh/rsms/inter@4.0/web/font-files/Inter-SemiBold.woff2"

ENV PYTHONUNBUFFERED=1

EXPOSE 9850 9851

LABEL version="1.0.0"

LABEL permissions='\
{\
  "ExposedPorts": {\
    "9850/tcp": {},\
    "9851/tcp": {}\
  },\
  "HostConfig": {\
    "Binds": [\
      "/usr/blueos/extensions/radcam-spy:/app/data"\
    ],\
    "ExtraHosts": ["host.docker.internal:host-gateway"],\
    "PortBindings": {\
      "9850/tcp": [\
        {\
          "HostPort": ""\
        }\
      ]\
    },\
    "NetworkMode": "host"\
  }\
}'

ARG AUTHOR
ARG AUTHOR_EMAIL
LABEL authors='[\
  {\
    "name": "Tony White",\
    "email": "tonywhite@bluerobotics.com"\
  }\
]'

ARG MAINTAINER
ARG MAINTAINER_EMAIL
LABEL company='{\
  "about": "Leading provider of marine robotics",\
  "name": "Blue Robotics",\
  "email": "support@bluerobotics.com"\
}'

LABEL type="tool"
LABEL tags="camera, monitoring, telnet, radcam, hisilicon"

ARG REPO
ARG OWNER
LABEL readme='https://raw.githubusercontent.com/${OWNER}/${REPO}/{tag}/README.md'
LABEL links='{\
  "source": "https://github.com/${OWNER}/${REPO}"\
}'

LABEL requirements="core >= 1.1"

ENTRYPOINT ["python3", "-u", "/app/main.py"]
