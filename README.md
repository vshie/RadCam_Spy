# RadCam Spy - BlueOS Extension

A BlueOS extension for monitoring RadCam IP cameras via telnet. Connects to the camera's HiSilicon SoC and periodically samples temperature, voltage, CPU usage, and memory usage.

## Features

- Telnet-based monitoring of HiSilicon camera internals
- Vue.js web interface for configuration and control
- Persistent NDJSON log files with download support
- Interactive graphs for reviewing collected data (temperature, CPU, memory)
- Cockpit-embeddable widget for quick start/stop control
- One-time credential setup with persistent storage

## Installation

### From BlueOS Extensions Manager

Install directly from the BlueOS Extensions Manager store once published.

### Manual Install

1. In BlueOS, go to Extensions Manager > Installed > "+"
2. Enter:
   - **Extension Identifier**: `bluerobotics.radcam-spy`
   - **Extension Name**: `RadCam Spy`
   - **Docker image**: `your-dockerhub-user/radcam-spy`
   - **Docker tag**: `main`

## Custom Settings (Permissions)

When manually installing, paste this into the **Custom settings** field:

```json
{
  "ExposedPorts": {
    "9850/tcp": {}
  },
  "HostConfig": {
    "Binds": [
      "/usr/blueos/extensions/radcam-spy:/app/data"
    ],
    "ExtraHosts": ["host.docker.internal:host-gateway"],
    "PortBindings": {
      "9850/tcp": [
        {
          "HostPort": ""
        }
      ]
    },
    "NetworkMode": "host"
  }
}
```

## Development

### Local Testing

```bash
docker-compose up --build
```

Then visit `http://localhost:9850` in your browser.

### GitHub Actions

The CI/CD pipeline requires these GitHub Secrets and Variables:

**Secrets:**
- `DOCKER_USERNAME` - Docker Hub username
- `DOCKER_PASSWORD` - Docker Hub access token (Read & Write)

**Variables:**
- `IMAGE_NAME` - Docker repository name (e.g. `radcam-spy`)
- `MY_NAME` - Author name
- `MY_EMAIL` - Author email
- `ORG_NAME` - Maintainer organization name
- `ORG_EMAIL` - Maintainer organization email

## License

MIT
