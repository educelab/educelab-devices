# Settings have no type field

A `Setting` doesn't declare its type. When it has `choices`, `validate()` returns the matching choice, so each accepted value carries its own type. Without choices, the type comes from `value`. When `value` is `None` and there are no choices, `validate()` can only check that the value is a number in range and returns it as a float. The driver's `_set_setting` then converts it to whatever the hardware takes, which it has to do anyway. A setting whose value can be `None` should list its choices where the hardware has them.

## Considered Options

- **A declared type on every `Setting`**: closes the `None`-without-choices gap, but a setting isn't always one type. A shutter list that offered `"Bulb"` alongside durations would need `float | str`, which the choices already say more precisely. Every driver would also have to fill it in for generic native settings, where today the type simply follows from the widget's value.
