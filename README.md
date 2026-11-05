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
python scripts/collect_openalex_journal.py --journal aanda --max-intervals 2 --max-requests 100
```

Prueba real acotada a una fecha de publicación:

```sh
python scripts/collect_openalex_journal.py --journal aanda --from-date 2026-10-01 --to-date 2026-10-01 --max-intervals 1 --max-requests 5
```

En GitHub Actions, lanzar manualmente **Astronomy & Astrophysics collector**.
Cada revista tiene cron activo cada cuatro horas, escalonado diez minutos en UTC. El límite interno es 25 minutos y el job tiene
30 minutos. Los intentos HTTP, descubrimiento y reintentos cuentan para el máximo.

## Datos y cobertura

`library/aanda.sqlite` guarda papers (campos aprobados y columnas consultables),
cobertura diaria, ejecuciones, estado y registro de llamadas sin secretos.
`library/aanda.coverage.sqlite` contiene una sola tabla `daily_coverage(day, articles, last_scan)`.
Las fechas son dias UTC; articles cuenta resultados del ultimo escaneo completo del dia.

Se consultan intervalos de hasta **120 dias de publicacion**, hasta ayer UTC,
con cursor y hasta 100 works por pagina. Una solicitud simultanea y un segundo
minimo entre llamadas. `--max-intervals` limita bloques, no peticiones.
Por defecto se intentan dos bloques: el reciente para refresco y otro historico.
`--interval-days` permite reducir el bloque (120 por defecto).

Solo se marca un intervalo completo al agotar sus paginas y comprobar IDs unicos
y conteos. Cada articulo conserva su fecha individual. Una interrupcion conserva
las paginas; el intervalo se reinicia con UPSERT en la siguiente ejecucion.

La cobertura publica es SQLite: daily_coverage contiene day (PK), articles y last_scan.
El articulo SQLite conserva el historial operativo y checkpoints por intervalo. Los huecos historicos se agrupan
en periodos contiguos de hasta 120 dias; luego se reconcilian intervalos antiguos.
Las altas tardias se detectan al volver a consultar sus fechas. El limite historico
guardado en collector_state debe revisarse si se incorporan obras anteriores.
No se usan filtros premium de actualizacion.

La seleccion persistida esta definida en `approved_fields.json`. `raw_json` contiene
solo los campos aprobados, proyectados y normalizados; ya no es el JSON original
integro de OpenAlex. Los Work/Source/Author IDs pierden el prefijo OpenAlex y los
DOI pierden https://doi.org/. Las URLs de acceso se mantienen completas. Los temas
conservan solo nombres; los autores conservan author.id. Los campos ausentes se
omiten y los valores nulos se mantienen.

La migracion automatica de esquema 3 adapta las filas antiguas en una transaccion,
sin peticiones a OpenAlex. Conserva first_seen_utc y last_seen_utc como metadatos
operativos del colector. La cobertura y el registro de peticiones no se proyectan.
Las respuestas de lista pueden limitar a 100 las autorias y ubicaciones mostradas;
no se solicita ampliacion individual automatica. No se borran articulos por su
ausencia posterior; los cambios de fuente/tipo requieren reconciliacion especifica.

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

## Revistas y horarios UTC

El orden del catalogo sigue la seleccion del usuario. El cron es ciclico: por ejemplo,
una fase de 04:00 incluye tambien 00:00. GitHub puede retrasar u omitir ejecuciones
programadas; la cobertura persistente recupera los huecos. Todos los colectores
comparten el grupo openalex-collectors y los limites conservadores de peticiones.
Cada revista conserva su SQLite y manifiesto propios, creados en su primera captura.

| Orden | Codigo | Revista | Cron UTC |
|---:|---|---|---|
| 1 | apj | The Astrophysical Journal | `0 3,7,11,15,19,23 * * *` |
| 2 | apjl | The Astrophysical Journal Letters | `10 3,7,11,15,19,23 * * *` |
| 3 | aj | The Astronomical Journal | `20 3,7,11,15,19,23 * * *` |
| 4 | pasp | Publications of the Astronomical Society of the Pacific | `30 3,7,11,15,19,23 * * *` |
| 5 | aanda | Astronomy & Astrophysics | `40 3,7,11,15,19,23 * * *` |
| 6 | mnras | Monthly Notices of the Royal Astronomical Society | `50 3,7,11,15,19,23 * * *` |
| 7 | natas | Nature Astronomy | `0 0,4,8,12,16,20 * * *` |
| 8 | jhep | Journal of High Energy Physics | `10 0,4,8,12,16,20 * * *` |
| 9 | epjc | European Physical Journal C | `20 0,4,8,12,16,20 * * *` |
| 10 | cmpa | Communications in Mathematical Physics | `30 0,4,8,12,16,20 * * *` |
| 11 | lmpa | Letters in Mathematical Physics | `40 0,4,8,12,16,20 * * *` |
| 12 | phrvd | Physical Review D | `50 0,4,8,12,16,20 * * *` |
| 13 | phrvl | Physical Review Letters | `0 1,5,9,13,17,21 * * *` |
| 14 | phlb | Physics Letters B | `10 1,5,9,13,17,21 * * *` |
| 15 | nuphb | Nuclear Physics B | `20 1,5,9,13,17,21 * * *` |
| 16 | cqgra | Classical and Quantum Gravity | `30 1,5,9,13,17,21 * * *` |
| 17 | jcap | Journal of Cosmology and Astroparticle Physics | `40 1,5,9,13,17,21 * * *` |
| 18 | jmp | Journal of Mathematical Physics | `50 1,5,9,13,17,21 * * *` |
| 19 | sci | SciPost Physics | `0 2,6,10,14,18,22 * * *` |
| 20 | mplb | Modern Physics Letters B | `10 2,6,10,14,18,22 * * *` |

## Cobertura diaria en SQLite

Cada revista tiene `library/<journal>.coverage.sqlite`, con una unica tabla:

```sql
CREATE TABLE daily_coverage (
  day TEXT PRIMARY KEY,
  articles INTEGER NOT NULL CHECK(articles >= 0),
  last_scan TEXT NOT NULL
);
```

Cada intervalo terminado actualiza sus dias, incluidos los de cero resultados.
Una captura parcial no cambia la cobertura diaria anterior. Los conteos se calculan
con los resultados de ese escaneo, no con la suma historica de papers. Las instantaneas
diarias se guardan en la base operativa y se exportan atomicamente al SQLite sencillo.
El workflow publica juntos ambos archivos; los JSON anteriores se retiraron.

En la conversion inicial, los archivos antiguos solo contenian totales por intervalo:
los conteos diarios se reconstruyeron desde los articulos almacenados y se conservaron
las fechas reales de finalizacion. Esa reconstruccion no reproduce una instantanea
historica exacta si los registros cambiaron despues. Nuevos escaneos completos
reemplazan esos conteos por los encontrados en la captura.

## Valoracion de abstracts

Dos workflows independientes: **Abstract importance Qwen mod0** y
**Abstract importance OpenScholar mod1**. Modelos GGUF CPU y revisiones fijadas
iguales al evaluador arXiv de referencia: Qwen3-1.7B Q4_K_M y
Llama-3.1 OpenScholar-8B Q4_K_M. No requieren claves ni consultas OpenAlex.

Se ejecutan manualmente, por repository_dispatch y cada ocho horas a las
02:00, 10:00 y 18:00 UTC. Cambiar priority_mod0.json o priority_mod1.json
activa solamente su modelo. Cada uno tiene concurrencia propia, timeout de
360 minutos y deja de iniciar valoraciones al minuto 345.

Los resultados van a abstract_evaluation/abstract_importance_mod0.sqlite y
abstract_importance_mod1.sqlite. Cada fila contiene work_id corto, importance
0-10, modelo, justificacion en espanol, revista, fecha de publicacion, hash del
abstract, momento de evaluacion y version del prompt. Las notas son estimaciones
de importancia potencial basadas exclusivamente en el abstract, no validaciones
cientificas ni una escala calibrada por expertos. No usan citas, nombres ni
prestigio de revistas para generar la nota.

La seleccion recorre las revistas configuradas, con preferencia por el dia de
publicacion mas reciente pendiente y rotacion entre revistas de ese dia. El
Work ID se deduplica globalmente dentro de cada modelo. Se omiten retractados,
abstracts vacios y abstracts que no caben completos en el contexto; no se truncan.
Las bibliotecas se leen como una instantanea al iniciar cada run, sin bloquear
las recolecciones que siguen escribiendo en otros runners.

Las prioridades son arrays de objetos con work_id y requested_at_utc. Se procesan
antes de la rotacion normal. Las ya valoradas se eliminan al iniciar el siguiente
run; las no disponibles o fallidas se conservan. Cinco fallos seguidos detienen
el evaluador y se guardan los avances disponibles.

Se publica despues de la primera valoracion y cada 30 minutos. Todas las conexiones
SQLite se cierran antes de un rebase; las fuentes no se mantienen abiertas durante
la inferencia. Cada modelo modifica solo sus propios resultados y prioridad.
Artifacts de respaldo se intentan guardar incluso en caso de fallo. Las
valoraciones ya guardadas no se recalculan automaticamente cuando cambia el abstract:
el hash permite detectar posteriormente esa necesidad.

## Colectores Crossref y Crossmark por revista

Hay 40 workflows: Crossref y Crossmark para cada una de las 20 revistas de
`config.json`. Usan el ISSN Crossref verificado de cada revista y filtran
`type:journal-article`. Se pueden lanzar manualmente, por cron cada cuatro horas
o por `repository_dispatch` del tipo `run-{crossref|crossmark}-{codigo}-collector`.
El cron Crossref ocurre tres minutos despues del OpenAlex correspondiente; el
Crossmark, siete minutos despues. Los cron de GitHub son aproximados.

Cada run recorre los 370 dias de publicacion mas recientes hasta ayer UTC,
ambos extremos incluidos, en intervalos de hasta 30 dias. Cada intervalo usa
cursor y `rows=1000` hasta completarse. `interval_coverage` se actualiza solo
cuando el intervalo termina; `runs` registra resultados y errores. Se puede
reintentar un intervalo incompleto en el siguiente run. Se detiene ante
paginacion inconsistente, 100 intentos HTTP o 24 minutos de captura.

Ambos modos conservan **todos** los campos del registro Crossref en
`library/crossref/{codigo}.sqlite` y `library/crossmark/{codigo}.sqlite`,
respectivamente. El JSON íntegro se comprime con zlib en `papers.raw_json_zlib`
para mantener cada archivo bajo el límite de GitHub; se recupera con
`json.loads(zlib.decompress(valor).decode('utf-8'))`. Las antiguas bases A&A
se migran automáticamente sin descartar campos. Crossmark usa la misma API
REST oficial; no es una API aparte.
La ausencia de sus campos de actualización no prueba que un artículo no haya
sido corregido o retractado. Esta ventana de publicacion no sustituye un futuro
escaneo por fecha de actualización para detectar cambios en artículos antiguos.

Los contactos `CROSSREF_MAILTO` y `CROSSMARK_MAILTO` son secretos de GitHub.
Los jobs espacian sus solicitudes un segundo, respetan `Retry-After` y guardan
artefactos incluso si falla un intervalo. Los grupos de concurrencia son por
modo y revista para evitar solapamiento del mismo archivo. Cada workflow solo
publica su SQLite si nadie lo cambió en el remoto desde su checkout. Las bases
manuales anteriores de A&A se conservan y se actualizarán con registros completos
durante la primera ejecución nueva.

Fuentes oficiales:
- https://www.crossref.org/documentation/retrieve-metadata/rest-api/
- https://www.crossref.org/documentation/retrieve-metadata/rest-api/rest-api-filters/
- https://www.crossref.org/crossmark/
