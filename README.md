# david_openalex

Colector incremental de metadatos OpenAlex para revistas seleccionadas.

La primera revista es **Astronomy & Astrophysics**, source `S205231332`, ISSN
0004-6361 / 1432-0746. Solo se incluyen works `type:article` cuya fuente primaria
es la revista. Se consulta el corpus core por defecto. No se descargan PDFs.

## Ejecutar

Python 3.12 y biblioteca estándar, sin dependencias externas. Configurar
`OPENALEX_API_KEY` en el entorno (localmente) o como secret del repo en Actions.
La clave nunca debe guardarse en Git. El PAT de GitHub es una credencial distinta.

```sh
python scripts/collect_openalex_journal.py --journal aanda --max-days 30 --max-requests 100
```

Prueba real acotada a una fecha de publicación:

```sh
python scripts/collect_openalex_journal.py --journal aanda --from-date 2026-10-01 --to-date 2026-10-01 --max-days 1 --max-requests 5
```

En GitHub Actions, lanzar manualmente **OpenAlex Astronomy & Astrophysics collector**.
No tiene cron activo inicialmente. El límite interno es 25 minutos y el job tiene
30 minutos. Los intentos HTTP, descubrimiento y reintentos cuentan para el máximo.

## Datos y cobertura

`library/aanda.sqlite` guarda papers (JSON recibido y columnas consultables),
cobertura diaria, ejecuciones, estado y registro de llamadas sin secretos.
`library/aanda.coverage.json` es un manifiesto generado desde esa base.

Se consultan días completos hasta ayer UTC, paginando con cursor, 100 works por
página, una solicitud simultánea y un segundo mínimo entre llamadas.
Un día solo queda `ok` tras completar las páginas y comprobar IDs únicos y conteos.
Un día sin resultados queda cubierto. Una interrupción conserva páginas recibidas;
el siguiente intento vuelve al inicio de ese día y hace UPSERT por Work ID.

Se revisitan hasta cinco días recientes por ejecución (reservando al menos una
plaza para huecos). Los huecos históricos se rellenan de más reciente a más
antiguo. Tras el backfill se reconcilia el histórico por antigüedad de consulta.
La fecha inicial detectada queda guardada en `collector_state`: debe revisarse
si se incorporan publicaciones anteriores al inicio conocido de la revista.
Las altas/correcciones tardías se detectan al revisitar sus fechas; no se promete
sincronización inmediata. No se usan filtros premium por fecha de actualización.

El JSON es el recibido en las respuestas de lista: OpenAlex puede limitar a 100
las autorías y ubicaciones mostradas. No equivale a descargar el registro individual
sin truncamientos. Los registros no se borran automáticamente si desaparecen de
una consulta posterior o cambian de fuente/tipo; esas diferencias requieren auditoría.

## Límites y errores

429 detiene conservadoramente el run, tanto si es cuota como si es ritmo.
401/403 son errores de autenticación/autorización. Errores de red/5xx se reintentan
con backoff y Retry-After, hasta tres reintentos, sujetos al máximo de intentos.
Un límite local deja el run `stopped`; un error de datos o servicio deja `error`.
El coste reportado se registra cuando está disponible. **Usar la API consume
presupuesto**; contar peticiones no garantiza un coste monetario máximo y la
cuota puede compartirse con otros clientes de la misma cuenta. No se compran
recargas ni se modifican planes.

Actions guarda un artefacto y publica SQLite/cobertura después de cerrar las
conexiones. Si detecta que los datos A&A cambiaron en remoto, conserva el artefacto
y falla la publicación en lugar de sobrescribirlos. Una terminación forzada del
runner puede perder cambios aún no guardados/publicados. Los cron pendientes no
son una cola fiable; la base de cobertura permite recuperar huecos.

## Comprobaciones locales sin consumo de API

```sh
python -m unittest discover -s tests -v
```

## Referencias

- [Autenticación y límites](https://help.openalex.org/api/authentication/)
- [Paginación](https://help.openalex.org/api/paging/)
- [Filtros](https://help.openalex.org/api/filtering/)
- [Precios](https://help.openalex.org/access/pricing/)
- [Campos Work](https://help.openalex.org/data/works/attributes/)

Arquitectura inspirada en `oliverscott-hub/oliver_arxiv`: cobertura diaria,
SQLite por unidad de recolección y workflow manual por revista.
