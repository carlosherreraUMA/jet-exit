# Datos para calibrar el modelo

Cada parámetro del modelo es un hueco para datos medidos. `python simulate_deboarding.py params`
lista todos con su valor actual, su unidad, de dónde sale y cómo medirlo. Este documento recoge
lo que dicen las fuentes encontradas y qué falta por medir.

Las distribuciones se escriben como tablas de cuantiles `((p, valor), ...)`, así que una muestra
medida se convierte directamente: se ordenan los valores y se toman, por ejemplo, los percentiles
0, 5, 10, 25, 50, 75, 90, 95 y 100. Se cambian con `--set`, por ejemplo:

```
python simulate_deboarding.py --set 'bag_time=[[0,2],[0.5,6],[0.9,12],[1,25]]'
```

## Qué dicen las fuentes

| Dato | Valor | Fuente | Uso en el modelo |
|---|---|---|---|
| Ritmo de desembarque por una puerta | 15-17 pax/min (media observada); 20 pax/min citado como media del sector | Wald, Harmon y Klabjan 2014, citado por CALM | Objetivo de calibración de la cortesía |
| Tiempo total, A319/A320 con ≥ 80 % de ocupación | 6:27 a 13:00 min | Vink et al. 2019 (estudio de campo) | Rango de validación |
| Tiempo total, A320 de 144 plazas | 8-10 min | CALM (Namilae et al.) citando datos observados | Rango de validación |
| Guardar el equipaje al embarcar | Weibull, forma 1,7 y escala 16 s (media ≈ 14 s, 323 mediciones) | Schultz 2018, Aerospace 5(1):27 | Forma de `bag_time` (sacar suele ser más rápido que guardar) |
| Bultos de mano por pasajero | 0 / 1 / 2 con 25 / 50 / 25 % | Schultz et al. 2023, JATM 106 | Orienta `p_bag` |
| Velocidad libre en el pasillo | 0,8 m/s | Schultz et al. 2023 | Mediana de `walk_speed` |
| Velocidad de peatón general | 1,1-1,3 m/s (σ 0,2) | CALM | Cota superior de `walk_speed` |
| Sacar el equipaje al desembarcar | 5-12 s (supuesto, no medido) | CALM | Contraste de `bag_time` |
| Maletas guardadas más atrás | Obligan a ir contra la corriente; es el cuello de botella principal | Vink et al. 2019 | Existencia de `bin_offsets` y la marcha atrás |
| Ir sin equipaje de mano | −80 % de tiempo de desembarque | Vink et al. 2019 | Validación de la sensibilidad a `p_bag` |

Una observación importante: los modelos publicados que se han revisado (CALM, Wald et al.) se
calibran solo con el tiempo total. Ninguno mide los tiempos microscópicos del desembarque
(sacar la maleta, levantarse, huecos que se aceptan). Esos datos habría que obtenerlos.

## Qué falta medir, por orden de impacto

1. **`bag_time`**: tiempo desde que se abre el compartimento hasta que la maleta está en el
   suelo del pasillo. Es el parámetro que más pesa.
2. **`p_prefetch`**: qué parte de los pasajeros con maleta la sacan antes de pisar el pasillo
   (de pie en la fila) y qué parte ya en el pasillo.
3. **`bin_offsets`**: fila del asiento frente a fila del compartimento donde está su maleta.
4. **`door_headway`**: salidas por minuto cuando hay cola continua en la puerta. Decide cuánto
   importa todo lo demás.
5. **`p_yield`, `gap_distance`**: en qué proporción de filas el pasillo se detiene para
   dejar salir, y qué huecos aceptan quienes salen sin que les cedan el paso.
6. **`s0`, `headway`, `start_gap`, `body_depth`**: personas por metro en una cola parada y
   ritmo al que se pone en marcha la cola (se mide contando cuánto tarda en moverse la
   persona número N tras la primera).
7. **`prep_time`, `stand_time`, `lateral_time`, `step_in_time`**: tiempos dentro de la fila.

## Cómo medirlo con vídeo

- **Fuente ideal**: un vídeo desde la parte delantera de la cabina mirando hacia atrás, o desde
  la última fila mirando hacia la puerta, con el desembarque completo. Hay bastantes en YouTube
  (por ejemplo, time-lapses de desembarque grabados por la tripulación); también sirve grabarlo
  uno mismo desde el asiento.
- **Registro**: una fila por evento, con la marca de tiempo del vídeo:
  `persona, fila, asiento, evento (se levanta / pisa el pasillo / abre el compartimento /
  maleta en el suelo / empieza a andar / sale), fila del compartimento, ¿le cedieron el paso?`.
- **Del registro a los parámetros**: los tiempos entre eventos dan directamente las tablas de
  cuantiles; la proporción de "le cedieron el paso" estima `p_yield`; las salidas por minuto
  en tramos con cola continua estiman `door_headway`.
- **Validación**: con 2-3 vídeos completos se compara el tiempo total y la curva de salidas
  acumuladas del modelo con la real, además del tiempo total medio.

## Referencias

- Schultz, M. (2018). Field Trial Measurements to Validate a Stochastic Aircraft Boarding Model.
  *Aerospace* 5(1):27. https://doi.org/10.3390/aerospace5010027
- Schultz, M. et al. (2023). A combined optimization–simulation approach for modified
  outside-in boarding. *Journal of Air Transport Management* 106:102258.
  https://www.unibw.de/lvk/publications/pdf/2022_jatm_boarding_bag.pdf
- Namilae, S. et al. Constrained Linear Movement Model (CALM): Simulation of passenger movement
  in airplanes. https://arxiv.org/pdf/1910.05749
- Wald, A., Harmon, M., Klabjan, D. (2014). Structured deplaning via simulation and
  optimization. *Journal of Air Transport Management* 36.
  https://www.sciencedirect.com/science/article/abs/pii/S0969699714000027
- Hiemstra-van Mastrigt, S., Ottens, R., Vink, P. (2019). Identifying bottlenecks and designing
  ideas and solutions for improving aircraft passengers' experience during boarding and
  disembarking. *Applied Ergonomics* 77. https://pubmed.ncbi.nlm.nih.gov/30832774/
