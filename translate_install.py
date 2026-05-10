import argostranslate.package

# Обновляем список доступных пакетов
argostranslate.package.update_package_index()

available_packages = argostranslate.package.get_available_packages()

# Нужные нам пары (греческий ↔ английский + английский ↔ русский)
needed_pairs = [
    ("el", "en"),   # греческий → английский
    ("en", "ru"),   # английский → русский
]

for from_code, to_code in needed_pairs:
    # Ищем пакет
    package = next(
        (pkg for pkg in available_packages 
         if pkg.from_code == from_code and pkg.to_code == to_code),
        None
    )
    
    if package is None:
        print(f"Пакет {from_code}-{to_code} не найден")
        continue
    
    print(f"Устанавливаем {from_code} → {to_code}...")
    try:
        # Скачиваем и устанавливаем
        path = package.download()
        argostranslate.package.install_from_path(path)
        print(f"✓ {from_code}-{to_code} установлен")
    except Exception as e:
        print(f"Ошибка при установке {from_code}-{to_code}: {e}")

print("Установка завершена!")