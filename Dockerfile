FROM python:3.11-slim

RUN pip install --no-cache-dir flask

WORKDIR /app
COPY app/ .

RUN mkdir -p /app/data/logs

ENV PYTHONUNBUFFERED=1

EXPOSE 9850

LABEL version="1.0.0"

LABEL permissions='\
{\
  "ExposedPorts": {\
    "9850/tcp": {}\
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
