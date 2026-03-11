import os

def create_project_structure():
    """
    Membuat struktur folder dan file untuk proyek FastAPI
    """
    
    # Definisi struktur proyek
    structure = {
        'app': {
            'files': [
                '__init__.py',
                'main.py',
                'config.py',
                'db.py',
                'models.py',
                'schemas.py',
                'utils.py'
            ],
            'folders': {
                'routers': {
                    'files': [
                        '__init__.py',
                        'health.py',
                        'upload.py',
                        'jobs.py'
                    ]
                },
                'services': {
                    'files': [
                        'http.py',
                        'blob.py',
                        'queue.py',
                        'translator.py'
                    ]
                }
            }
        },
        'worker': {
            'files': [
                '__init__.py',
                'worker.py'
            ]
        }
    }
    
    def create_folders_and_files(base_path, structure_dict):
        """
        Fungsi rekursif untuk membuat folder dan file
        """
        for folder_name, folder_content in structure_dict.items():
            folder_path = os.path.join(base_path, folder_name)
            
            # Buat folder jika belum ada
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
                print(f"✅ Folder dibuat: {folder_path}")
            else:
                print(f"📁 Folder sudah ada: {folder_path}")
            
            # Buat file-file di dalam folder
            if 'files' in folder_content:
                for file_name in folder_content['files']:
                    file_path = os.path.join(folder_path, file_name)
                    if not os.path.exists(file_path):
                        with open(file_path, 'w', encoding='utf-8') as f:
                            # Menambahkan komentar dasar untuk setiap file
                            if file_name.endswith('.py'):
                                f.write(f'"""\n{file_name} - Module untuk proyek\n"""\n\n')
                        print(f"📄 File dibuat: {file_path}")
                    else:
                        print(f"📄 File sudah ada: {file_path}")
            
            # Proses subfolder jika ada
            if 'folders' in folder_content:
                create_folders_and_files(folder_path, folder_content['folders'])
    
    try:
        # Mulai membuat struktur dari direktori saat ini
        current_dir = os.getcwd()
        print(f"🚀 Membuat struktur proyek di: {current_dir}\n")
        
        create_folders_and_files(current_dir, structure)
        
        print("\n✨ Struktur proyek berhasil dibuat!")
        
        # Tampilkan struktur yang telah dibuat
        print("\n📋 Struktur yang dibuat:")
        print_tree_structure(current_dir, structure)
        
    except Exception as e:
        print(f"❌ Error saat membuat struktur: {e}")

def print_tree_structure(base_path, structure_dict, prefix=""):
    """
    Menampilkan struktur folder dalam format tree
    """
    items = list(structure_dict.keys())
    for i, folder_name in enumerate(items):
        is_last = i == len(items) - 1
        current_prefix = "└─ " if is_last else "├─ "
        print(f"{prefix}{current_prefix}{folder_name}/")
        
        folder_content = structure_dict[folder_name]
        next_prefix = prefix + ("   " if is_last else "│  ")
        
        # Print files
        if 'files' in folder_content:
            files = folder_content['files']
            for j, file_name in enumerate(files):
                is_last_file = j == len(files) - 1 and 'folders' not in folder_content
                file_prefix = "└─ " if is_last_file else "├─ "
                print(f"{next_prefix}{file_prefix}{file_name}")
        
        # Print subfolders
        if 'folders' in folder_content:
            print_tree_structure(base_path, folder_content['folders'], next_prefix)

if __name__ == "__main__":
    create_project_structure()