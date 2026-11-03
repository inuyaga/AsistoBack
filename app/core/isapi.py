import base64
from datetime import datetime, timedelta
from typing import Any, Optional, TypedDict
from urllib.parse import urlparse
import uuid
import xml.etree.ElementTree as ET

import httpx


class ISAPIError(Exception):
    """Base exception raised when communication with a Hikvision ISAPI endpoint fails."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = message
        self.status_code = status_code


class ImageNotSuport(ISAPIError):
    """Raised when the provided face image is not a valid JPEG or cannot be decoded."""


class NetworkNotFountInterface(ISAPIError):
    """Raised when /ISAPI/System/Network/interfaces does not return valid network interfaces."""


class SubpicAnalysisModelingError(ISAPIError):
    """Raised when the Hikvision terminal fails to model the face image (FDSetUp)."""


class UserNotAdd(ISAPIError):
    """Raised when a user cannot be created or updated in the Hikvision device."""


class UserNotFound(ISAPIError):
    """Raised when the requested employeeNo does not exist on the Hikvision device."""


class UserNotAddApiException(ISAPIError):
    """API-level exception when adding/updating a user or face fails on the device."""


class NetworkInterfaceDict(TypedDict, total=False):
    id: str
    ip_address: Optional[str]
    subnet_mask: Optional[str]
    default_gateway: Optional[str]
    mac_address: Optional[str]
    addressing_type: Optional[str]
    primary_dns: Optional[str]
    secondary_dns: Optional[str]
    mtu: Optional[str]
    speed: Optional[str]
    duplex: Optional[str]


class NetworkInterfaceListDict(TypedDict):
    interfaces: list[NetworkInterfaceDict]
    raw: dict[str, Any]


# Mapa de sub-eventos (minor) comunes de Hikvision Access Control (major = 5)
ACS_MINOR_EVENT_LABELS: dict[int, str] = {
    1: "Tarjeta válida",
    2: "Tarjeta y contraseña válidas",
    4: "Tarjeta fuera de horario",
    8: "Tarjeta no registrada / inválida",
    9: "Tarjeta expirada",
    21: "Puerta abierta por botón",
    22: "Puerta abierta",
    23: "Puerta cerrada",
    38: "Huella dactilar verificada",
    39: "Huella dactilar fallida",
    75: "Reconocimiento facial exitoso",
    76: "Reconocimiento facial fallido",
    105: "Contraseña verificada",
}


def _strip_xml_namespaces(elem: ET.Element) -> ET.Element:
    """Remove XML namespace prefixes in-place for clean tag lookup."""
    for el in elem.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return elem


def _xml_text(root: ET.Element, tag: str, default: Optional[str] = None) -> Optional[str]:
    node = root.find(f".//{tag}")
    if node is not None and node.text is not None:
        cleaned = node.text.strip()
        return cleaned if cleaned else default
    return default


class IsapiGenerateXmlController:
    """Generates ISAPI XML payloads and converts XML responses into Python dictionaries."""

    @staticmethod
    def capture_face_data_xml() -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<CaptureFaceDataCond version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            "<captureInfrared>false</captureInfrared>"
            "<dataType>url</dataType>"
            "</CaptureFaceDataCond>"
        )

    @staticmethod
    def notifications_register_data(
        id_notifications: int | str,
        url_api: str,
        protocol_type: str,
        address_format_type: str,
        port: int | str,
        ip_address: Optional[str] = None,
        hostname: Optional[str] = None,
        parameter_format_type: str = "JSON",
        auth_method: str = "none",
        auth_username: Optional[str] = None,
        auth_password: Optional[str] = None,
        wrap_in_list: bool = False,
    ) -> str:
        clean_path = url_api.strip() if url_api.strip().startswith("/") else f"/{url_api.strip()}"
        proto = (protocol_type or "HTTP").upper().strip()
        addr_fmt = (address_format_type or "ipaddress").lower().strip()
        param_fmt = (parameter_format_type or "JSON").upper().strip()

        if addr_fmt == "hostname":
            target_host = (hostname or ip_address or "").strip()
            addr_xml = f"<hostName>{target_host}</hostName>"
        else:
            target_ip = (ip_address or hostname or "").strip()
            addr_xml = f"<ipAddress>{target_ip}</ipAddress>"

        auth_xml = f"<httpAuthenticationMethod>{auth_method}</httpAuthenticationMethod>"
        if auth_method != "none" and auth_username:
            auth_xml += f"<userName>{auth_username}</userName>"
            if auth_password:
                auth_xml += f"<password>{auth_password}</password>"

        inner = (
            '<HttpHostNotification version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<id>{id_notifications}</id>"
            f"<url>{clean_path}</url>"
            f"<protocolType>{proto}</protocolType>"
            f"<parameterFormatType>{param_fmt}</parameterFormatType>"
            f"<addressingFormatType>{addr_fmt}</addressingFormatType>"
            f"{addr_xml}"
            f"<portNo>{port}</portNo>"
            f"{auth_xml}"
            "</HttpHostNotification>"
        )

        if wrap_in_list:
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<HttpHostNotificationList version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
                f"{inner}"
                "</HttpHostNotificationList>"
            )
        return f'<?xml version="1.0" encoding="UTF-8"?>{inner}'

    @classmethod
    def _element_to_dict(cls, element: ET.Element) -> Any:
        children = list(element)
        if not children:
            return (element.text or "").strip()

        result: dict[str, Any] = {}
        for child in children:
            child_data = cls._element_to_dict(child)
            if child.tag in result:
                if not isinstance(result[child.tag], list):
                    result[child.tag] = [result[child.tag]]
                result[child.tag].append(child_data)
            else:
                result[child.tag] = child_data
        return result

    @classmethod
    def xml_to_dict(cls, text: str) -> dict[str, Any]:
        """Parse an XML string (stripping namespaces) into a nested dictionary."""
        cleaned = text.strip()
        if not cleaned:
            return {}
        try:
            root = _strip_xml_namespaces(ET.fromstring(cleaned))
        except ET.ParseError as exc:
            raise ISAPIError(f"Respuesta XML ISAPI inválida: {exc}") from exc
        return {root.tag: cls._element_to_dict(root)}


class HikvisionISAPIClient:
    """Client for Hikvision biometric / access control terminals via ISAPI (HTTP Digest Auth)."""

    xml_controller = IsapiGenerateXmlController()

    def __init__(
        self,
        ip_address: str,
        port: int = 80,
        username: str = "admin",
        password: str = "",
        protocol: str = "http",
        verify_ssl: bool = False,
        timeout_seconds: int = 8,
    ) -> None:
        self.ip_address = ip_address.strip()
        self.port = port
        self.username = username.strip()
        self.password = password
        self.protocol = protocol.lower().strip()
        self.verify_ssl = verify_ssl
        self.timeout_seconds = max(2, min(timeout_seconds, 30))
        self.base_url = f"{self.protocol}://{self.ip_address}:{self.port}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        xml_body: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
        files: Optional[dict[str, Any]] = None,
        timeout_override: Optional[float] = None,
    ) -> httpx.Response:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        headers: dict[str, str] = {
            "User-Agent": "AttendanceSystem-ISAPI/1.0",
        }
        content: Optional[bytes] = None
        if xml_body is not None:
            headers["Content-Type"] = "application/xml; charset=UTF-8"
            content = xml_body.encode("utf-8")

        effective_timeout = timeout_override or float(self.timeout_seconds)

        try:
            with httpx.Client(
                auth=httpx.DigestAuth(self.username, self.password),
                verify=self.verify_ssl,
                timeout=httpx.Timeout(effective_timeout),
                follow_redirects=True,
            ) as client:
                response = client.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    content=content,
                    data=data,
                    files=files,
                    headers=headers,
                )
                if response.status_code == 401 and "basic" in response.headers.get(
                    "www-authenticate", ""
                ).lower():
                    response = client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                        content=content,
                        data=data,
                        files=files,
                        headers=headers,
                        auth=httpx.BasicAuth(self.username, self.password),
                    )
        except httpx.ConnectTimeout as exc:
            raise ISAPIError(
                f"Tiempo de espera agotado al conectar con {self.base_url} ({effective_timeout}s)",
                status_code=504,
            ) from exc
        except httpx.ReadTimeout as exc:
            raise ISAPIError(
                f"El dispositivo {self.base_url} tardó demasiado en responder ({effective_timeout}s)",
                status_code=504,
            ) from exc
        except httpx.ConnectError as exc:
            raise ISAPIError(
                f"No se pudo establecer conexión con {self.base_url}. Verifica IP, puerto y red.",
                status_code=502,
            ) from exc
        except httpx.HTTPError as exc:
            raise ISAPIError(
                f"Error de transporte HTTP hacia {self.base_url}: {exc}",
                status_code=502,
            ) from exc

        if response.status_code == 401:
            raise ISAPIError(
                "Autenticación ISAPI fallida (HTTP 401). Verifica el usuario y contraseña del reloj.",
                status_code=401,
            )
        if response.status_code == 403:
            raise ISAPIError(
                "Acceso denegado por el dispositivo (HTTP 403). Verifica que ISAPI esté habilitado en el reloj.",
                status_code=403,
            )
        if response.status_code >= 400:
            detail = self._extract_error_detail(response)
            raise ISAPIError(
                f"Respuesta de error ISAPI (HTTP {response.status_code}): {detail}",
                status_code=response.status_code,
            )
        return response

    @staticmethod
    def _extract_error_detail(response: httpx.Response) -> str:
        text = response.text.strip()
        if not text:
            return response.reason_phrase or "Error desconocido"
        try:
            data = response.json()
            if isinstance(data, dict):
                sub_code = data.get("subStatusCode") or data.get("statusString")
                error_msg = data.get("errorMsg")
                if sub_code or error_msg:
                    return f"{sub_code or ''} {error_msg or ''}".strip()
        except Exception:
            pass
        try:
            root = _strip_xml_namespaces(ET.fromstring(text))
            status_str = _xml_text(root, "statusString")
            sub_code = _xml_text(root, "subStatusCode")
            if status_str or sub_code:
                return f"{status_str or ''} ({sub_code or ''})".strip()
        except Exception:
            pass
        return text[:200]

    def health_check(self) -> str:
        """Check connectivity via GET /ISAPI/Event/notification/httpHosts (fallback /ISAPI/System/deviceInfo)."""
        try:
            resp = self._request("GET", "/ISAPI/Event/notification/httpHosts")
            return f"OK: {resp.status_code}"
        except ISAPIError:
            resp = self._request("GET", "/ISAPI/System/deviceInfo")
            return f"OK: {resp.status_code}"

    def get_device_info(self) -> dict[str, Optional[str]]:
        """Query GET /ISAPI/System/deviceInfo and parse hardware/firmware metadata."""
        response = self._request("GET", "/ISAPI/System/deviceInfo")
        text = response.text.strip()
        try:
            root = _strip_xml_namespaces(ET.fromstring(text))
        except ET.ParseError as exc:
            raise ISAPIError(
                "El dispositivo respondió pero no devolvió un XML ISAPI válido en /ISAPI/System/deviceInfo"
            ) from exc

        return {
            "device_name": _xml_text(root, "deviceName"),
            "device_id": _xml_text(root, "deviceID"),
            "model": _xml_text(root, "model"),
            "serial_number": _xml_text(root, "serialNumber"),
            "mac_address": _xml_text(root, "macAddress"),
            "firmware_version": _xml_text(root, "firmwareVersion"),
            "firmware_released_date": _xml_text(root, "firmwareReleasedDate"),
            "device_type": _xml_text(root, "deviceType"),
            "manufacturer": _xml_text(root, "manufacturer", default="Hikvision"),
        }

    def get_networks_interfaces_info(self) -> NetworkInterfaceListDict:
        """Query GET /ISAPI/System/Network/interfaces and return parsed network interfaces."""
        response = self._request("GET", "/ISAPI/System/Network/interfaces")
        parsed = self.xml_controller.xml_to_dict(response.text)
        net_list = parsed.get("NetworkInterfaceList")
        if not isinstance(net_list, dict):
            raise NetworkNotFountInterface(
                "No se encontró NetworkInterfaceList en la respuesta ISAPI del reloj"
            )

        raw_ifaces = net_list.get("NetworkInterface", [])
        if isinstance(raw_ifaces, dict):
            raw_ifaces = [raw_ifaces]
        elif not isinstance(raw_ifaces, list):
            raw_ifaces = []

        if not raw_ifaces:
            raise NetworkNotFountInterface(
                "El dispositivo no reportó interfaces de red en /ISAPI/System/Network/interfaces"
            )

        interfaces: list[NetworkInterfaceDict] = []
        for iface in raw_ifaces:
            if not isinstance(iface, dict):
                continue
            ip_node = iface.get("IPAddress", {}) if isinstance(iface.get("IPAddress"), dict) else {}
            gw_node = (
                ip_node.get("DefaultGateway", {})
                if isinstance(ip_node.get("DefaultGateway"), dict)
                else {}
            )
            dns1_node = (
                ip_node.get("PrimaryDNS", {})
                if isinstance(ip_node.get("PrimaryDNS"), dict)
                else {}
            )
            dns2_node = (
                ip_node.get("SecondaryDNS", {})
                if isinstance(ip_node.get("SecondaryDNS"), dict)
                else {}
            )
            link_node = iface.get("Link", {}) if isinstance(iface.get("Link"), dict) else {}

            interfaces.append(
                {
                    "id": str(iface.get("id", "1")),
                    "ip_address": ip_node.get("ipAddress") or ip_node.get("ipv6Address"),
                    "subnet_mask": ip_node.get("subnetMask"),
                    "default_gateway": gw_node.get("ipAddress"),
                    "addressing_type": ip_node.get("addressingType"),
                    "primary_dns": dns1_node.get("ipAddress"),
                    "secondary_dns": dns2_node.get("ipAddress"),
                    "mac_address": link_node.get("MACAddress"),
                    "mtu": link_node.get("MTU"),
                    "speed": link_node.get("speed"),
                    "duplex": link_node.get("duplex"),
                }
            )

        return {
            "interfaces": interfaces,
            "raw": parsed,
        }

    def get_time_info(self) -> dict[str, Optional[str]]:
        """Query GET /ISAPI/System/time and return device clock settings."""
        response = self._request("GET", "/ISAPI/System/time")
        try:
            root = _strip_xml_namespaces(ET.fromstring(response.text.strip()))
            return {
                "time_mode": _xml_text(root, "timeMode"),
                "local_time": _xml_text(root, "localTime"),
                "time_zone": _xml_text(root, "timeZone"),
            }
        except ET.ParseError:
            return {"time_mode": None, "local_time": None, "time_zone": None}

    def sync_time(
        self,
        target_time: Optional[datetime] = None,
        time_zone: Optional[str] = None,
    ) -> dict[str, Optional[str]]:
        """Synchronize the Hikvision device's internal clock via PUT /ISAPI/System/time."""
        current_info: dict[str, Optional[str]] = {}
        try:
            current_info = self.get_time_info()
        except ISAPIError:
            pass

        tz = time_zone or current_info.get("time_zone") or "CST+6:00:00"
        dt = target_time or datetime.now().astimezone()
        formatted_time = dt.strftime("%Y-%m-%dT%H:%M:%S")

        xml_payload = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Time version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            "<timeMode>manual</timeMode>"
            f"<localTime>{formatted_time}</localTime>"
            f"<timeZone>{tz}</timeZone>"
            "</Time>"
        )
        self._request("PUT", "/ISAPI/System/time", xml_body=xml_payload)
        return {
            "time_mode": "manual",
            "local_time": formatted_time,
            "time_zone": tz,
        }

    def get_user_count(self) -> Optional[int]:
        """Try to query total users stored on the terminal via ISAPI AccessControl."""
        try:
            resp = self._request(
                "GET",
                "/ISAPI/AccessControl/UserInfo/Count",
                params={"format": "json"},
            )
            data = resp.json()
            count_obj = data.get("UserInfoCount", data)
            if isinstance(count_obj, dict) and "userNumber" in count_obj:
                return int(count_obj["userNumber"])
        except Exception:
            pass
        return None

    def probe_device(self) -> dict[str, Any]:
        """Run a full diagnostic probe against the Hikvision device via ISAPI."""
        info = self.get_device_info()
        time_info: dict[str, Optional[str]] = {
            "time_mode": None,
            "local_time": None,
            "time_zone": None,
        }
        try:
            time_info = self.get_time_info()
        except ISAPIError:
            pass

        if not info.get("mac_address"):
            try:
                net_info = self.get_networks_interfaces_info()
                if net_info["interfaces"]:
                    info["mac_address"] = net_info["interfaces"][0].get("mac_address")
            except Exception:
                pass

        user_count = self.get_user_count()
        return {
            **info,
            "time_mode": time_info.get("time_mode"),
            "local_time": time_info.get("local_time"),
            "time_zone": time_info.get("time_zone"),
            "user_count": user_count,
        }

    def reboot(self) -> str:
        """Send PUT /ISAPI/System/reboot to restart the device."""
        response = self._request("PUT", "/ISAPI/System/reboot")
        try:
            root = _strip_xml_namespaces(ET.fromstring(response.text.strip()))
            status_str = _xml_text(root, "statusString", default="OK")
            return status_str or "OK"
        except Exception:
            return "OK"

    def control_door(self, door_id: int = 1, cmd: str = "open") -> str:
        """Send PUT /ISAPI/AccessControl/RemoteControl/door/{door_id}."""
        xml_payload = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            f"<cmd>{cmd}</cmd>"
            "</RemoteControlDoor>"
        )
        self._request(
            "PUT",
            f"/ISAPI/AccessControl/RemoteControl/door/{door_id}",
            xml_body=xml_payload,
        )
        return f"Comando '{cmd}' enviado a puerta #{door_id}"

    # -------------------------------------------------------------------------
    # Gestión de Empleados y Biometría Facial (según README_ISAPI.md)
    # -------------------------------------------------------------------------

    def add_employee(
        self,
        employee_no: str,
        name: str,
        user_type: str = "normal",
        begin_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a user on the terminal via POST /ISAPI/AccessControl/UserInfo/Record?format=json."""
        now = datetime.now().astimezone()
        start_str = begin_time or now.strftime("%Y-%m-%dT%H:%M:%S")
        end_str = end_time or (now + timedelta(days=3650)).strftime("%Y-%m-%dT23:59:59")

        payload = {
            "UserInfo": {
                "employeeNo": str(employee_no).strip(),
                "name": name.strip(),
                "userType": user_type,
                "Valid": {
                    "enable": True,
                    "beginTime": start_str,
                    "endTime": end_str,
                    "timeType": "local",
                },
                "doorRight": "1",
                "RightPlan": [
                    {
                        "doorNo": 1,
                        "planTemplateNo": "1",
                    }
                ],
            }
        }
        try:
            resp = self._request(
                "POST",
                "/ISAPI/AccessControl/UserInfo/Record",
                params={"format": "json"},
                json_body=payload,
            )
            data = resp.json() if resp.text.strip() else {}
            status_code = data.get("statusCode", 1)
            if status_code != 1 and data.get("statusString") not in (None, "OK"):
                raise UserNotAdd(
                    f"El reloj rechazó el alta del empleado {employee_no}: {data.get('subStatusCode') or data.get('statusString')}"
                )
            return {
                "employee_no": str(employee_no).strip(),
                "name": name.strip(),
                "begin_time": start_str,
                "end_time": end_str,
                "status": "OK",
            }
        except ISAPIError as exc:
            raise UserNotAdd(f"No se pudo agregar al empleado '{employee_no}': {exc.message}", exc.status_code) from exc

    def update_employee(
        self,
        employee_no: str,
        name: str,
        user_type: str = "normal",
        begin_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> dict[str, Any]:
        """Modify an existing user via PUT /ISAPI/AccessControl/UserInfo/Modify?format=json."""
        now = datetime.now().astimezone()
        start_str = begin_time or now.strftime("%Y-%m-%dT00:00:00")
        end_str = end_time or (now + timedelta(days=3650)).strftime("%Y-%m-%dT23:59:59")

        payload = {
            "UserInfo": {
                "employeeNo": str(employee_no).strip(),
                "name": name.strip(),
                "userType": user_type,
                "Valid": {
                    "enable": True,
                    "beginTime": start_str,
                    "endTime": end_str,
                    "timeType": "local",
                },
                "doorRight": "1",
                "RightPlan": [
                    {
                        "doorNo": 1,
                        "planTemplateNo": "1",
                    }
                ],
            }
        }
        try:
            resp = self._request(
                "PUT",
                "/ISAPI/AccessControl/UserInfo/Modify",
                params={"format": "json"},
                json_body=payload,
            )
            data = resp.json() if resp.text.strip() else {}
            if data.get("statusCode", 1) != 1 and data.get("statusString") not in (None, "OK"):
                raise UserNotFound(
                    f"No se pudo actualizar el empleado {employee_no}: {data.get('subStatusCode') or data.get('statusString')}"
                )
            return {
                "employee_no": str(employee_no).strip(),
                "name": name.strip(),
                "status": "OK",
            }
        except ISAPIError as exc:
            raise UserNotAddApiException(
                f"No se pudo modificar el empleado '{employee_no}': {exc.message}",
                exc.status_code,
            ) from exc

    # Alias compatible con README_ISAPI.md
    update_employe = update_employee

    def upsert_employee(
        self,
        employee_no: str,
        name: str,
        user_type: str = "normal",
        begin_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create or update an employee on the Hikvision terminal (tries Record first, falls back to Modify)."""
        try:
            res = self.add_employee(
                employee_no=employee_no,
                name=name,
                user_type=user_type,
                begin_time=begin_time,
                end_time=end_time,
            )
            return {**res, "operation": "created"}
        except ISAPIError as create_err:
            if create_err.status_code in (401, 502, 504):
                raise
            try:
                res = self.update_employee(
                    employee_no=employee_no,
                    name=name,
                    user_type=user_type,
                    begin_time=begin_time,
                    end_time=end_time,
                )
                return {**res, "operation": "updated"}
            except ISAPIError:
                raise create_err

    def baja_employee(self, employee_nos: list[str]) -> dict[str, Any]:
        """Delete one or more users via PUT /ISAPI/AccessControl/UserInfo/Delete?format=json."""
        cleaned_nos = [str(no).strip() for no in employee_nos if str(no).strip()]
        if not cleaned_nos:
            raise UserNotFound("Debes indicar al menos un número de empleado para dar de baja")

        payload = {
            "UserInfoDelCond": {
                "EmployeeNoList": [{"employeeNo": no} for no in cleaned_nos]
            }
        }
        resp = self._request(
            "PUT",
            "/ISAPI/AccessControl/UserInfo/Delete",
            params={"format": "json"},
            json_body=payload,
        )
        return {
            "deleted_employee_nos": cleaned_nos,
            "status_code": resp.status_code,
            "status": "OK",
        }

    def save_face_image(self, base64_img: str, employee_no: str) -> dict[str, Any]:
        """Upload a JPEG face image for an employee via PUT /ISAPI/Intelligent/FDLib/FDSetUp?format=json."""
        raw_str = (base64_img or "").strip()
        if not raw_str:
            raise ImageNotSuport("No se proporcionó ninguna imagen en base64")

        if "," in raw_str and raw_str.startswith("data:"):
            header, b64_data = raw_str.split(",", 1)
            if "image/jpeg" not in header.lower() and "image/jpg" not in header.lower():
                raise ImageNotSuport(
                    "Formato de imagen no soportado. El dispositivo Hikvision requiere imagen JPEG (data:image/jpeg;base64,...)."
                )
        else:
            b64_data = raw_str

        try:
            image_bytes = base64.b64decode(b64_data, validate=False)
        except Exception as exc:
            raise ImageNotSuport("La cadena base64 de la imagen facial es inválida") from exc

        # Verificar firma mágica JPEG (\xff\xd8\xff)
        if len(image_bytes) < 4 or not image_bytes.startswith(b"\xff\xd8\xff"):
            raise ImageNotSuport(
                "El archivo decodificado no es un JPEG válido. Usa una fotografía en formato JPG/JPEG."
            )

        face_data_record = (
            f'{{"faceLibType":"blackFD","FDID":"1","FPID":"{str(employee_no).strip()}"}}'
        )
        files = {
            "FaceDataRecord": (None, face_data_record, "application/json"),
            "img": (f"{employee_no}.jpg", image_bytes, "image/jpeg"),
        }

        try:
            resp = self._request(
                "PUT",
                "/ISAPI/Intelligent/FDLib/FDSetUp",
                params={"format": "json"},
                files=files,
                timeout_override=15.0,
            )
        except ISAPIError as exc:
            if "modeling" in exc.message.lower() or "subpic" in exc.message.lower():
                raise SubpicAnalysisModelingError(
                    f"El reloj rechazó la fotografía porque no pudo modelar el rostro: {exc.message}",
                    exc.status_code,
                ) from exc
            raise

        try:
            body = resp.json()
            if isinstance(body, dict):
                sub_code = str(body.get("subStatusCode") or "")
                if body.get("statusCode", 1) != 1 and sub_code not in ("", "ok"):
                    raise SubpicAnalysisModelingError(
                        f"El dispositivo no pudo modelar el rostro ({sub_code}): {body.get('errorMsg') or body.get('statusString')}"
                    )
        except SubpicAnalysisModelingError:
            raise
        except Exception:
            pass

        return {
            "employee_no": str(employee_no).strip(),
            "status": "OK",
            "message": f"Rostro JPEG registrado correctamente para el empleado {employee_no}",
        }

    def _get_image_file_safe(self, file_url: str) -> bytes:
        """Download captured face image from the device while preventing SSRF (README_ISAPI.md #7.3)."""
        parsed = urlparse(file_url)
        if parsed.scheme and parsed.hostname:
            if parsed.hostname.lower() != self.ip_address.lower():
                raise ISAPIError(
                    f"URL de imagen rechazada por seguridad (SSRF): el host '{parsed.hostname}' no coincide con el dispositivo '{self.ip_address}'."
                )
            path_and_query = parsed.path
            if parsed.query:
                path_and_query += f"?{parsed.query}"
        else:
            path_and_query = file_url

        resp = self._request("GET", path_and_query, timeout_override=15.0)
        return resp.content

    def capture_face_data(self) -> dict[str, Any]:
        """Trigger live facial capture on the device via POST /ISAPI/AccessControl/CaptureFaceData."""
        xml_payload = self.xml_controller.capture_face_data_xml()
        resp = self._request(
            "POST",
            "/ISAPI/AccessControl/CaptureFaceData",
            xml_body=xml_payload,
            timeout_override=20.0,
        )

        content_type = resp.headers.get("content-type", "").lower()
        if "image/jpeg" in content_type or resp.content.startswith(b"\xff\xd8\xff"):
            b64_str = base64.b64encode(resp.content).decode("ascii")
            return {
                "base64_image": f"data:image/jpeg;base64,{b64_str}",
                "face_url": None,
                "capture_progress": 100,
            }

        parsed = self.xml_controller.xml_to_dict(resp.text)
        cap_node = parsed.get("CaptureFaceData", {})
        if not isinstance(cap_node, dict):
            raise ISAPIError("Respuesta inesperada en /ISAPI/AccessControl/CaptureFaceData")

        face_url = cap_node.get("faceDataUrl")
        progress = int(cap_node.get("captureProgress", 100) or 100)

        if not face_url or not isinstance(face_url, str):
            raise ISAPIError(
                "El reloj no devolvió faceDataUrl. Asegúrate de que la persona esté frente a la cámara del terminal."
            )

        image_bytes = self._get_image_file_safe(face_url)
        b64_str = base64.b64encode(image_bytes).decode("ascii")
        return {
            "base64_image": f"data:image/jpeg;base64,{b64_str}",
            "face_url": face_url,
            "capture_progress": progress,
        }

    def search_users(
        self,
        search_position: int = 0,
        max_results: int = 30,
        employee_no: Optional[str] = None,
    ) -> dict[str, Any]:
        """Search users registered on the Hikvision device via POST /ISAPI/AccessControl/UserInfo/Search?format=json."""
        cond: dict[str, Any] = {
            "searchID": str(uuid.uuid4()),
            "searchResultPosition": max(0, search_position),
            "maxResults": max(1, min(max_results, 100)),
        }
        if employee_no and employee_no.strip():
            cond["EmployeeNoList"] = [{"employeeNo": employee_no.strip()}]

        response = self._request(
            "POST",
            "/ISAPI/AccessControl/UserInfo/Search",
            params={"format": "json"},
            json_body={"UserInfoSearchCond": cond},
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise ISAPIError("El dispositivo no devolvió JSON válido al consultar usuarios") from exc

        search_data = payload.get("UserInfoSearch", payload) if isinstance(payload, dict) else {}
        raw_users = search_data.get("UserInfo", [])
        if isinstance(raw_users, dict):
            raw_users = [raw_users]
        elif not isinstance(raw_users, list):
            raw_users = []

        users: list[dict[str, Any]] = []
        for u in raw_users:
            if not isinstance(u, dict):
                continue
            valid_info = u.get("Valid", {}) if isinstance(u.get("Valid"), dict) else {}
            users.append(
                {
                    "employee_no": str(u.get("employeeNo", "")),
                    "name": str(u.get("name", "")),
                    "user_type": str(u.get("userType", "normal")),
                    "num_of_card": int(u.get("numOfCard", 0) or 0),
                    "num_of_fp": int(u.get("numOfFP", 0) or 0),
                    "num_of_face": int(u.get("numOfFace", 0) or 0),
                    "valid_enabled": bool(valid_info.get("enable", True)),
                    "begin_time": valid_info.get("beginTime"),
                    "end_time": valid_info.get("endTime"),
                }
            )

        return {
            "total_matches": int(search_data.get("totalMatches", len(users)) or 0),
            "response_status": str(search_data.get("responseStatusStrg", "OK")),
            "users": users,
        }

    def search_events(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        major: int = 5,
        minor: int = 0,
        search_position: int = 0,
        max_results: int = 30,
        employee_no: Optional[str] = None,
        fetch_all_pages: bool = False,
    ) -> dict[str, Any]:
        """Search attendance events via POST /ISAPI/AccessControl/AcsEvent?format=json (supports 30-block pagination while MORE)."""
        if fetch_all_pages:
            return self.filter_events(
                start_time=start_time,
                end_time=end_time,
                major=major,
                minor=minor,
                employee_no=employee_no,
            )

        return self._search_events_page(
            start_time=start_time,
            end_time=end_time,
            major=major,
            minor=minor,
            search_position=search_position,
            max_results=max_results,
            employee_no=employee_no,
        )

    def _search_events_page(
        self,
        *,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        major: int = 5,
        minor: int = 0,
        search_position: int = 0,
        max_results: int = 30,
        employee_no: Optional[str] = None,
        search_id: Optional[str] = None,
    ) -> dict[str, Any]:
        cond: dict[str, Any] = {
            "searchID": search_id or str(uuid.uuid4()),
            "searchResultPosition": max(0, search_position),
            "maxResults": max(1, min(max_results, 30)),
            "major": major,
            "minor": minor,
        }
        if start_time:
            cond["startTime"] = start_time
        if end_time:
            cond["endTime"] = end_time
        if employee_no and employee_no.strip():
            cond["employeeNoString"] = employee_no.strip()

        response = self._request(
            "POST",
            "/ISAPI/AccessControl/AcsEvent",
            params={"format": "json"},
            json_body={"AcsEventCond": cond},
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise ISAPIError("El dispositivo no devolvió JSON válido al consultar eventos AcsEvent") from exc

        acs_data = payload.get("AcsEvent", payload) if isinstance(payload, dict) else {}
        raw_events = acs_data.get("InfoList", [])
        if isinstance(raw_events, dict):
            raw_events = [raw_events]
        elif not isinstance(raw_events, list):
            raw_events = []

        events: list[dict[str, Any]] = []
        for ev in raw_events:
            if not isinstance(ev, dict):
                continue
            ev_major = int(ev.get("major", major) or major)
            ev_minor = int(ev.get("minor", 0) or 0)
            events.append(
                {
                    "serial_no": ev.get("serialNo"),
                    "employee_no": ev.get("employeeNoString") or ev.get("employeeNo"),
                    "name": ev.get("name"),
                    "card_no": ev.get("cardNo"),
                    "time": ev.get("time"),
                    "major": ev_major,
                    "minor": ev_minor,
                    "event_label": ACS_MINOR_EVENT_LABELS.get(ev_minor, f"Evento ({ev_major}:{ev_minor})"),
                    "attendance_status": ev.get("attendanceStatus"),
                    "verify_mode": ev.get("currentVerifyMode"),
                    "door_no": ev.get("doorNo"),
                }
            )

        return {
            "total_matches": int(acs_data.get("totalMatches", len(events)) or 0),
            "response_status": str(acs_data.get("responseStatusStrg", "OK")),
            "events": events,
        }

    def filter_events(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        major: int = 5,
        minor: int = 0,
        employee_no: Optional[str] = None,
        max_pages: int = 20,
    ) -> dict[str, Any]:
        """Paginate events in blocks of 30 until responseStatusStrg != 'MORE' (README_ISAPI.md #6 & #7.6)."""
        all_events: list[dict[str, Any]] = []
        position = 0
        block_size = 30
        search_id = str(uuid.uuid4())
        total_matches = 0
        last_status = "OK"

        for _ in range(max_pages):
            page = self._search_events_page(
                start_time=start_time,
                end_time=end_time,
                major=major,
                minor=minor,
                search_position=position,
                max_results=block_size,
                employee_no=employee_no,
                search_id=search_id,
            )
            batch = page["events"]
            all_events.extend(batch)
            total_matches = page["total_matches"]
            last_status = page["response_status"]

            if last_status.upper() != "MORE" or not batch:
                break
            position += block_size

        return {
            "total_matches": total_matches or len(all_events),
            "response_status": last_status,
            "events": all_events,
        }

    # -------------------------------------------------------------------------
    # Notificaciones HTTP (HttpHostNotification: /ISAPI/Event/notification/httpHosts)
    # -------------------------------------------------------------------------

    def get_http_host_notification(self, host_id: int = 2) -> dict[str, Any]:
        """Read HTTP Listening configuration via GET /ISAPI/Event/notification/httpHosts (fallback triggers)."""
        response: Optional[httpx.Response] = None
        for candidate_path in (
            f"/ISAPI/Event/notification/httpHosts/{host_id}",
            "/ISAPI/Event/notification/httpHosts",
            f"/ISAPI/Event/triggers/httpHosts/{host_id}",
            "/ISAPI/Event/triggers/httpHosts",
        ):
            try:
                response = self._request("GET", candidate_path)
                break
            except ISAPIError:
                continue

        if response is None:
            raise ISAPIError(
                "No se pudo consultar /ISAPI/Event/notification/httpHosts en el dispositivo"
            )

        try:
            root = _strip_xml_namespaces(ET.fromstring(response.text.strip()))
        except ET.ParseError as exc:
            raise ISAPIError(
                "El dispositivo no devolvió un XML válido en /ISAPI/Event/notification/httpHosts"
            ) from exc

        node = root if root.tag == "HttpHostNotification" else root.find(".//HttpHostNotification")
        if node is None:
            return {
                "host_id": host_id,
                "url_path": None,
                "protocol_type": None,
                "parameter_format_type": None,
                "addressing_format_type": None,
                "ip_address": None,
                "domain": None,
                "host_address": None,
                "port_no": None,
                "auth_method": None,
            }

        addr_type = _xml_text(node, "addressingFormatType", default="ipaddress")
        ip_addr = _xml_text(node, "ipAddress")
        host_name = _xml_text(node, "hostName")
        host_address = host_name if addr_type == "hostname" and host_name else (ip_addr or host_name)
        port_text = _xml_text(node, "portNo")

        return {
            "host_id": int(_xml_text(node, "id") or host_id),
            "url_path": _xml_text(node, "url"),
            "protocol_type": _xml_text(node, "protocolType", default="HTTP"),
            "parameter_format_type": _xml_text(node, "parameterFormatType", default="JSON"),
            "addressing_format_type": addr_type,
            "ip_address": ip_addr,
            "domain": host_name,
            "host_address": host_address,
            "port_no": int(port_text) if port_text and port_text.isdigit() else None,
            "auth_method": _xml_text(node, "httpAuthenticationMethod", default="none"),
            "auth_username": _xml_text(node, "userName"),
        }

    def set_http_host_notification(
        self,
        *,
        host_id: int = 2,
        url_path: str = "/api/v1/devices/webhook/isapi",
        protocol_type: str = "HTTP",
        parameter_format_type: str = "JSON",
        addressing_format_type: str = "ipaddress",
        host_address: str = "192.168.1.100",
        ip_address: Optional[str] = None,
        domain: Optional[str] = None,
        port_no: int = 8000,
        auth_method: str = "none",
        auth_username: Optional[str] = None,
        auth_password: Optional[str] = None,
    ) -> dict[str, Any]:
        """Configure HTTP Listening on the device via PUT /ISAPI/Event/notification/httpHosts/{id} (README_ISAPI.md)."""
        effective_ip = ip_address or (host_address if addressing_format_type == "ipaddress" else None)
        effective_domain = domain or (host_address if addressing_format_type == "hostname" else None)

        single_xml = self.xml_controller.notifications_register_data(
            id_notifications=host_id,
            url_api=url_path,
            protocol_type=protocol_type,
            address_format_type=addressing_format_type,
            port=port_no,
            ip_address=effective_ip,
            hostname=effective_domain,
            parameter_format_type=parameter_format_type,
            auth_method=auth_method,
            auth_username=auth_username,
            auth_password=auth_password,
            wrap_in_list=False,
        )
        list_xml = self.xml_controller.notifications_register_data(
            id_notifications=host_id,
            url_api=url_path,
            protocol_type=protocol_type,
            address_format_type=addressing_format_type,
            port=port_no,
            ip_address=effective_ip,
            hostname=effective_domain,
            parameter_format_type=parameter_format_type,
            auth_method=auth_method,
            auth_username=auth_username,
            auth_password=auth_password,
            wrap_in_list=True,
        )

        # Intentar primero el endpoint oficial del README_ISAPI.md: /ISAPI/Event/notification/httpHosts/{id}
        attempts: list[tuple[str, str]] = [
            (f"/ISAPI/Event/notification/httpHosts/{host_id}", single_xml),
            ("/ISAPI/Event/notification/httpHosts", list_xml),
            (f"/ISAPI/Event/triggers/httpHosts/{host_id}", single_xml),
            ("/ISAPI/Event/triggers/httpHosts", list_xml),
        ]

        last_err: Optional[ISAPIError] = None
        for endpoint, xml_payload in attempts:
            try:
                self._request("PUT", endpoint, xml_body=xml_payload)
                last_err = None
                break
            except ISAPIError as err:
                last_err = err
                if err.status_code in (401, 403, 502, 504):
                    break

        if last_err is not None:
            raise last_err

        clean_path = url_path.strip() if url_path.strip().startswith("/") else f"/{url_path.strip()}"
        target_host = (
            effective_domain
            if addressing_format_type == "hostname" and effective_domain
            else (effective_ip or host_address)
        )
        return {
            "host_id": host_id,
            "target_url": f"{protocol_type.lower()}://{target_host.strip()}:{port_no}{clean_path}",
            "parameter_format_type": parameter_format_type,
        }

    # Alias compatible con el nombre en README_ISAPI.md
    register_server_reciber_notifications = set_http_host_notification


# Alias de clase compatible con README_ISAPI.md
IsapiController = HikvisionISAPIClient
