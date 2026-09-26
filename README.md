# ¿Quién cede el pasillo?

Simulación del desembarque de un avión de pasillo único (A320, 30 filas 3-3) para responder a
una pregunta concreta: **¿saldríamos antes si quien ya está de pie en el pasillo no se parase
a dejar salir a cada fila, y los que siguen sentados esperasen un hueco?**

La regla es un único parámetro, `p_yield`: la probabilidad de que alguien que camina por el
pasillo se pare a dejar salir a una fila. `1` es la cortesía habitual; `0`, el pasillo primero.

## Resultado actual

Con 100 aviones simulados y los valores por defecto, la cortesía tarda 12:34 y el pasillo
primero 6:48: la cortesía es un 85 % más lenta. La ventaja se mantiene en todos los parámetros
probados y se reduce sobre todo si la puerta es lenta (3 s por pasajero: +35 %). Basta con que
cedan el paso uno de cada diez para perder cerca de la mitad de la ventaja.

Muchos parámetros son todavía estimaciones. El modelo está calibrado contra el ritmo total
observado (15-17 pasajeros por minuto), pero faltan mediciones de detalle; ver [DATA.md](DATA.md)
y la sección de datos más abajo.

## Qué hay aquí

| Fichero | Qué es |
|---|---|
| `simulate_deboarding.py` | El modelo y el CLI de Monte Carlo (Python, sin dependencias). |
| `calibrate.py` | Convierte anotaciones de vídeos reales en parámetros del modelo y valida. |
| `animate_deboarding.py` | Animación en la terminal de las dos reglas a la vez. |
| `web/index.html` | Simulación en directo, curvas y Monte Carlo en el navegador. |
| `web/cabin3d.html` | Las dos cabinas en 3D (three.js). |
| `web/annotate.html` | Herramienta para anotar vídeos de desembarques reales. |
| `web/model.js` | Port del modelo a JavaScript, idéntico bit a bit al de Python. |
| `sweep.js` | Barrido de sensibilidad sobre el port JS (unas 20 veces más rápido). |
| `DATA.md` | Fuentes, parámetros pendientes de medir y protocolo de medida. |

## Uso

```
python simulate_deboarding.py --k 300 --seed 1        # cortesía frente a pasillo primero
python simulate_deboarding.py params                  # todos los parámetros, con su fuente
python simulate_deboarding.py --set p_bag=0.4          # cambiar cualquier parámetro
python animate_deboarding.py --rows 12                # animación en la terminal
node sweep.js --k 100 --csv sweep.csv                 # barrido de sensibilidad
```

Para las páginas web basta con servir la carpeta `web/` (por ejemplo,
`python -m http.server -d web`) y abrir `http://localhost:8000`.

## El modelo, en breve

- Pasillo continuo en metros; cada persona tiene su profundidad de cuerpo, su velocidad y
  aceleración limitada, y guarda distancia con quien tiene delante. Las colas arrancan en ola.
- Dentro de la fila sale primero el de pasillo; los demás se levantan y se desplazan de lado.
- Las maletas pueden estar encima de su fila, delante o detrás. Quien la tiene detrás y fuera de
  su alcance espera a que se vacíe el pasillo, lo reserva y vuelve contra la corriente.
- Todos los parámetros están en `Config`, con unidad, fuente y cómo medirlos. Las
  distribuciones son tablas de cuantiles, para meter datos medidos tal cual.

La descripción completa está al principio de `simulate_deboarding.py`.

## Aportar datos

Lo que más mejoraría el modelo son mediciones de desembarques reales: cuánto se tarda en sacar
una maleta, cuántos la sacan desde su fila, dónde está respecto a su asiento, cuándo se cede el
paso. Si tienes un vídeo en el que se vea el pasillo:

1. Anótalo con `web/annotate.html` (el vídeo no sale de tu ordenador).
2. Copia el CSV y comprueba qué se puede estimar: `python calibrate.py tu-vuelo.csv`.
3. Aporta el CSV (no el vídeo): es anónimo, solo contiene tiempos y números de asiento.

## Tests

```
python -m pytest test_simulation.py test_calibrate.py
node web/parity_test.js            # el port JS reproduce a Python bit a bit
```

Tras cambiar el modelo: `python export_parity.py` regenera `web/data.js` (configuración por
defecto y casos de referencia) y `node web/parity_test.js` comprueba la paridad.
