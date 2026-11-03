# IsapiController – Guía para replicarlo en otro proyecto

Cliente Python para dispositivos biométricos/control de acceso **Hikvision** vía **ISAPI** (HTTP + Digest Auth).
Permite alta/baja/edición de usuarios, registro de rostros, consulta de eventos, captura facial y configuración de notificaciones.

## 1. Requisitos

- Python 3.9+
- Django (solo se usa `django.utils.timezone` y los modelos; fácil de desacoplar)
- `requests`

```bash
pip install requests django
```

## 2. Archivos a copiar

| Archivo | Rol |
|---|---|
| `controllers/isapi_controller.py` | Cliente principal (`IsapiController`) |
| `controllers/isapi_generate_xml_controller.py` | Genera XML y convierte XML→dict (`IsapiGenerateXmlController`) |
| `custom_exceptions/isapi_exceptions.py` | Excepciones propias |
| `typed_dict/isapi_dict.py` | Tipos (`NetworkInterfaceListDict`) |
| `models.py` (parcial) | `Device` y `ConfigServerNotificationsRecibe` |

Ajusta los imports `apps.biometricos...` a la ruta del nuevo proyecto.

## 3. Contratos que debe cumplir el nuevo proyecto

### Modelo `Device`
```python
class Device(models.Model):
    url = models.CharField(max_length=255)   # ej. "http://192.168.1.50" (sin "/" final)
    user = models.CharField(max_length=100)
    password = models.CharField(max_length=255)  # idealmente cifrada
```

### Modelo `ConfigServerNotificationsRecibe`
Campos usados: `end_point`, `protocol`, `address_type`, `port`, `ip_address`, `domain`.

### Excepciones (`isapi_exceptions.py`)
`ImageNotSuport`, `NetworkNotFountInterface`, `SubpicAnalysisModelingError`,
`UserNotAdd`, `UserNotFound`, `UserNotAddApiException`
(las dos últimas aceptan `detail=` / `message=`; pueden heredar de `APIException` de DRF).

### `IsapiGenerateXmlController`
Métodos requeridos:
- `capture_face_data_xml() -> str`
- `notifications_register_data(id_notifications, url_api, protocol_type, address_format_type, port, ip_address, hostname) -> str`
- `xml_to_dict(text: str) -> dict`

## 4. Uso

```python
from apps.biometricos.models import Device
from apps.biometricos.controllers.isapi_controller import IsapiController

device = Device.objects.get(pk=1)
isapi = IsapiController(device)

isapi.health_check()                         # "OK: 200" o mensaje de error
isapi.add_employee("1001", "Juan Pérez")     # alta de usuario
isapi.update_employe("1001", "Juan P.")      # cambia nombre
isapi.save_face_image(base64_img, "1001")    # base64 con prefijo "data:image/jpeg;base64,..."
face = isapi.capture_face_data()             # captura desde el dispositivo (base64)
events = isapi.filter_events(
    "2025-01-01T00:00:00-06:00", "2025-01-31T23:59:59-06:00", result=[]
)
isapi.baja_employee(["1001"])                # elimina usuarios
isapi.register_server_reciber_notifications()
info = isapi.get_networks_interfaces_info()
```

## 5. Endpoints ISAPI utilizados

| Método | HTTP | Endpoint |
|---|---|---|
| `capture_face_data` | POST | `/ISAPI/AccessControl/CaptureFaceData` |
| `add_employee` | POST | `/ISAPI/AccessControl/UserInfo/Record?format=json` |
| `update_employe` | PUT | `/ISAPI/AccessControl/UserInfo/Modify?format=json` |
| `baja_employee` | PUT | `/ISAPI/AccessControl/UserInfo/Delete?format=json` |
| `save_face_image` | PUT | `/ISAPI/Intelligent/FDLib/FDSetUp?format=json` (multipart) |
| `filter_events` | POST | `/ISAPI/AccessControl/AcsEvent?format=json` |
| `health_check` | GET | `/ISAPI/Event/notification/httpHosts` |
| `register_server_reciber_notifications` | PUT | `/ISAPI/Event/notification/httpHosts/2` |
| `get_networks_interfaces_info` | GET | `/ISAPI/System/Network/interfaces` |

## 6. Notas importantes

- **Autenticación:** `HTTPDigestAuth(device.user, device.password)`.
- **Paginación de eventos:** bloques de 30 (`maxResults`) hasta que `responseStatusStrg` deje de ser `MORE`.
- **Imágenes:** el rostro debe ser JPEG; el dispositivo rechaza imágenes que no pueda modelar (`SubpicAnalysisModelingError`).
- **Fechas:** `add_employee` usa `timezone.now()`; confirma que coincida con la hora local del equipo.
- **Red:** el servidor debe tener alcance a la IP del dispositivo (VPN/LAN).

## 7. Mejoras recomendadas al portarlo

1. Centralizar peticiones en un `_request()` con `timeout` y `requests.Session`.
2. Capturar `requests.RequestException` y lanzar una excepción propia.
3. Validar que `get_image_file(url)` apunte al host del dispositivo (evitar SSRF).
4. Soportar HTTPS (`verify`) configurable por dispositivo.
5. Eliminar `print`, imports sin usar (`BytesIO`) y corregir nombres (`ramdom_search_id`, `update_employe`, `postion`).
6. Evitar `result=[]` mutable compartido en `filter_events`; crear la lista dentro de la función.
7. Cifrar `Device.password` en base de datos.
8. Añadir pruebas con `unittest.mock` / `responses`.
