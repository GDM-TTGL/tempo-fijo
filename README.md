# Tempo Fijo

Aplicación de escritorio para Windows que analiza MP3 y crea una copia con el tempo alineado al BPM elegido o a dos BPM en una transición.

## Instalación de usuario

El usuario final recibirá un solo archivo: `TempoFijoSetup.exe`. El instalador coloca la aplicación y sus componentes de audio; no pide instalar Python, FFmpeg ni paquetes por separado. Se instala en el perfil del usuario y no requiere permisos de administrador.

## Modos

1. **Analizar variaciones:** estima el BPM por tramos y guarda un reporte de texto junto al archivo.
2. **Corregir a BPM fijo:** crea una copia y ajusta sus pulsos al BPM seleccionado.
3. **Corregir una transición:** crea una copia con un BPM a cada lado del cambio, que se alinea a un pulso detectado.

Los archivos originales se conservan. Reanaliza la copia en Rekordbox y escucha cada resultado antes de usarlo; la detección es una estimación y el procesamiento puede producir artefactos.

## Actualizaciones

La aplicación consulta la versión más reciente del repositorio configurado, descarga el instalador, verifica su suma SHA-256 e inicia la actualización. Para activar esta función, publica los instaladores y sus archivos `.sha256` como activos de cada GitHub Release. La configuración de publicación se genera durante la compilación.

## Compilar el instalador

En una máquina Windows de compilación, instala Python 3.11 o posterior e Inno Setup 6, y ejecuta `build_windows.ps1`. Para publicar una versión con actualizaciones, configura el proyecto en GitHub y crea una etiqueta `vX.Y.Z`; el flujo de publicación genera y sube el instalador.

Los usuarios finales no necesitan ninguna de estas herramientas de compilación.
