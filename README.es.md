# xiaomi-scale-gw

**Sincroniza tu Xiaomi Body Composition Scale S400 con Xiaomi Home sin abrir la app**, usando una Raspberry Pi Pico W como gateway Bluetooth → WiFi.

[Read in English](README.md)

Te subes a la báscula y unos segundos después la pesada está en Xiaomi Home con la composición corporal completa (grasa, músculo, metabolismo basal, puntuación…), igual que si la hubiera sincronizado la app.

> [!WARNING]
> Proyecto no oficial, sin relación con Xiaomi ni Yunmai. Usa **APIs de Xiaomi Cloud no documentadas, obtenidas por ingeniería inversa**, que pueden cambiar o dejar de funcionar en cualquier momento. Guarda en la Pico un token de sesión de tu cuenta Xiaomi. Úsalo bajo tu responsabilidad.

## Cómo funciona

```
 Báscula S400 ──BLE (MiBeacon cifrado)──▶ Pico W ──HTTPS──▶ Xiaomi Cloud ──▶ app Xiaomi Home
```

1. Tras cada pesada, la S400 emite anuncios Bluetooth cifrados (MiBeacon v5, objeto `0x6E16`). Llevan el peso, el pulso, las dos impedancias (50 kHz / 250 kHz), el perfil de usuario y la hora de la báscula.
2. La Pico W escucha en modo pasivo, los descifra con la *bindkey* de la báscula (AES-CCM) y agrupa los paquetes en una pesada.
3. Después hace en Xiaomi Cloud exactamente lo mismo que el plugin oficial de la báscula:
   - `/eco/scale/account/list`: busca el miembro de la familia que corresponde al perfil de la báscula.
   - `/eco/scale/bcalc`: Xiaomi calcula la composición corporal a partir de las impedancias.
   - `/eco/common/scale/add`: guarda la pesada que ves en la app.
   - `/eco/scale/account/updateWeight`: actualiza el peso de referencia con el que la báscula te reconoce.
   - Las pesadas de alguien no reconocido van a la lista de **datos sin reclamar** de la app, igual que hace la app.

El registro usa la hora de la propia báscula. En las pruebas, abrir la app después (que sincroniza la memoria de la báscula) **no** creó duplicados.

## Probado con

| | |
|---|---|
| Gateway | Raspberry Pi Pico W (RP2040) con MicroPython 1.29.0 |
| Báscula | Xiaomi Body Composition Scale S400, `MJTZC01YM` (`yunmai.scales.ms104`) |
| Cuenta Xiaomi | Servidor de Europa (`de`) |

Sin probar: otras variantes de la S400 (p. ej. `yunmai.scales.ms103` de China), otros servidores y la Pico 2 W. Se agradecen los informes en Issues.

## Requisitos

- Raspberry Pi Pico W y un cable USB **de datos**.
- Un ordenador con Python 3.10+ (Windows, macOS o Linux), solo para la configuración.
- La báscula ya añadida en la app **Xiaomi Home**, con tu perfil de usuario creado en su plugin (pésate una vez con la app).
- Una red WiFi de 2,4 GHz.

## Instalación

### 1. Descarga el código y las herramientas del PC

```bash
git clone https://github.com/DaniCP/xiaomi-scale-gw.git
cd xiaomi-scale-gw
python -m pip install -r requirements.txt
```

### 2. Flashea MicroPython en la Pico W

1. Descarga el último `.uf2` para **Pico W** de [micropython.org/download/RPI_PICO_W](https://micropython.org/download/RPI_PICO_W/).
2. Mantén pulsado el botón **BOOTSEL** mientras conectas la Pico por USB. Aparece una unidad llamada `RPI-RP2`.
3. Copia el `.uf2` en esa unidad. La Pico se reinicia con MicroPython.

### 3. Ejecuta el asistente de configuración

```bash
python tools/setup_gateway.py --deploy
```

El asistente:

1. Muestra un **código QR**. Escanéalo con la app Xiaomi Home para iniciar sesión. Tu contraseña de Xiaomi nunca se escribe ni se guarda.
2. Busca tu S400 en la cuenta y descarga su bindkey.
3. Te pide el nombre y la contraseña del WiFi.
4. Genera `secrets/config.json` y copia el firmware y la configuración a la Pico (`--deploy`).

Si tienes varias placas conectadas, añade `--port COM5` (Windows) o `--port /dev/ttyACM0` (Linux/macOS).

### 4. Úsalo

Enchufa la Pico a cualquier cargador USB cerca de la báscula (a pocos metros) y pésate como siempre.

> [!IMPORTANT]
> Si la app Xiaomi Home está **abierta y conectada** a la báscula, la báscula le envía los datos al móvil en vez de emitirlos. No pasa nada (la app los guarda), pero la Pico no los verá. El gateway sirve para cuando no abres la app.

## LED

| LED | Significado |
|---|---|
| Un destello largo | Arrancando |
| Parpadeo rápido | Conectando al WiFi |
| Parpadeo irregular | Recibiendo datos de la báscula |
| Dos destellos largos | Pesada guardada en Xiaomi Cloud |
| Tres destellos cortos, repetidos | Falta `config.json`: ejecuta la configuración |

## Logs y diagnóstico

Ver el log del gateway por USB (solo lectura, no detiene el gateway):

```bash
python -m serial.tools.miniterm COM5 115200
```

```
17:35:40 scale: profile 1 weight 70.8 kg hr 74 imp 474.9 raw 01c4c2344aec3ba46a
17:35:41 scale: weighing done 70.8 kg hr 74 imp 474.9/423.1 complete=True objs=2
17:35:46 cloud: upload 70.8 kg -> OK {"createTime": 1789148140000, "bfp": 19.7, "composition": true}
```

Consultar desde el PC lo que hay guardado en la nube:

```bash
python tools/cloud_probe.py records      # últimas pesadas
python tools/cloud_probe.py unclaimed    # pesadas pendientes de reclamar
```

## Configuración

El asistente genera `secrets/config.json`. [`secrets.example/config.json`](secrets.example/config.json) muestra su formato:

| Clave | Descripción |
|---|---|
| `wifi.ssid`, `wifi.password` | Red WiFi de 2,4 GHz |
| `xiaomi.region` | Servidor de Xiaomi: `de`, `cn`, `i2`, `us`, `ru`, `sg`… |
| `xiaomi.user_id`, `xiaomi.pass_token` | Cuenta Xiaomi y token de sesión de larga duración (del login por QR) |
| `xiaomi.device_id` | Identificador aleatorio del gateway ante Xiaomi |
| `scale.mac`, `scale.did`, `scale.model`, `scale.sn` | Identificadores de la báscula en tu cuenta Xiaomi |
| `scale.bindkey` | Clave de 32 caracteres hexadecimales para descifrar los datos Bluetooth de la báscula |

Para desplegar cambios a mano: `python tools/deploy.py --config secrets/config.json`.

## Seguridad y privacidad

- **`pass_token` da acceso completo a tu cuenta Xiaomi.** Se guarda en texto plano en `secrets/config.json` (ignorado por git) y en la memoria flash de la Pico. Cualquiera con acceso físico a la Pico puede leerlo. No publiques nunca ese archivo ni el contenido de la Pico.
- Para revocar el acceso, cierra la sesión en todos los dispositivos desde tu cuenta Xiaomi (o cambia la contraseña) y vuelve a ejecutar la configuración.
- MicroPython en la Pico no verifica los certificados TLS, así que una red local maliciosa podría interceptar el tráfico. Úsalo solo en una red de confianza.
- El gateway solo se comunica con servidores de Xiaomi. No envía nada a ningún otro sitio.

## Solución de problemas

| Problema | Solución |
|---|---|
| No aparecen líneas `scale:` en el log | Probablemente la app está conectada a la báscula: ciérrala o apaga el Bluetooth del móvil. Mantén la Pico a pocos metros. |
| `scale: frame error bad key/mic` | Bindkey incorrecta (p. ej. se volvió a añadir la báscula a la app): repite la configuración. |
| `cloud: error ... passToken rejected` | El token de sesión caducó o se revocó: repite la configuración. |
| La pesada aparece como *sin reclamar* | La báscula no reconoció a la persona (perfil 0). Reclámala en la app; tras unas cuantas pesadas la báscula aprende. |
| No aparece el puerto serie, o aparece la unidad `RPI-RP2` | La Pico está en modo BOOTSEL: vuelve a copiar el `.uf2` de MicroPython. Tus archivos se conservan. |

## Desarrollo

```
pico/            Firmware MicroPython (se ejecuta en la Pico W)
  main.py          WiFi, escaneo BLE, cola de subida, LED
  mibeacon.py      Parser MiBeacon + descifrado AES-CCM
  scale.py         Agrupa los paquetes de la S400 en pesadas
  uploader.py      Guarda una pesada igual que el plugin oficial
  xmcloud.py       Cliente mínimo de Xiaomi Cloud (login con passToken, API firmada con RC4)
tools/           Herramientas del PC (configuración, despliegue, diagnóstico)
tests/           Tests unitarios (se ejecutan en el PC)
secrets.example/ Plantilla de configuración
```

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

Los módulos del firmware también funcionan en CPython; así los ejercitan los tests.

## Créditos

Este proyecto se apoya en el trabajo de ingeniería inversa de:

- [Bluetooth-Devices/xiaomi-ble](https://github.com/Bluetooth-Devices/xiaomi-ble): formato MiBeacon y estructura del objeto de la S400.
- [PiotrMachowski/Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor): flujos de login de Xiaomi y API firmada con RC4.
- [AlexxIT/SmartScaleConnect](https://github.com/AlexxIT/SmartScaleConnect): endpoints de Xiaomi Cloud para básculas.
- [al-one/hass-xiaomi-miot](https://github.com/al-one/hass-xiaomi-miot): detalles de la API de Xiaomi Cloud.
- [MicroPython](https://micropython.org/).

## Contribuciones

Este repositorio no acepta contribuciones. Puedes **hacer un fork** y adaptarlo a lo que necesites; consulta [CONTRIBUTING.md](CONTRIBUTING.md).

## Licencia

[MIT](LICENSE). "Xiaomi" y "Xiaomi Home" son marcas de Xiaomi Inc.; aquí solo se mencionan para indicar compatibilidad.
