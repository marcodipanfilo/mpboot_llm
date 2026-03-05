"""
Patterns Discovery
Parse patterns.json to extract pattern information for specific tables.
"""

import json
import os
from typing import Dict, Any, List, Optional


def load_patterns(patterns_file: str = "src2/memory/patterns.json") -> Dict[str, Any]:
    """
    Load patterns from JSON file
    
    Args:
        patterns_file: Path to patterns JSON file
        
    Returns:
        Patterns dictionary
    """
    if not os.path.exists(patterns_file):
        raise FileNotFoundError(f"Patterns file not found: {patterns_file}")
    
    with open(patterns_file, 'r') as f:
        patterns = json.load(f)
    
    return patterns


def get_table_pattern_type(table_name: str, patterns: Dict[str, Any]) -> Optional[str]:
    """
    Get the pattern type for a specific table
    
    Args:
        table_name: Name of the table
        patterns: Patterns dictionary
        
    Returns:
        Pattern type string (e.g., "SE", "SR", "SE_SH") or None if not found
    """
    return patterns.get('table_patterns', {}).get(table_name)


def get_pattern_definition(pattern_type: str, patterns: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Get the definition for a specific pattern type
    
    Args:
        pattern_type: Pattern type (e.g., "SE", "SEw", "SR", "SRR", "SH")
        patterns: Patterns dictionary
        
    Returns:
        Pattern definition dictionary or None if not found
    """
    # Handle SE_SH case - it's SE with additional SH
    base_pattern = pattern_type.replace('_SH', '') if '_SH' in pattern_type else pattern_type
    
    return patterns.get('pattern_definitions', {}).get(base_pattern)


def get_sh_relation_info(table_name: str, patterns: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Get subclass hierarchy information if table is involved in SH relation
    
    Args:
        table_name: Name of the table
        patterns: Patterns dictionary
        
    Returns:
        SH relation info or None if table is not in SH relation
    """
    sh_relations = patterns.get('SH_relations', [])
    
    # Check if table is a child in any SH relation
    for sh in sh_relations:
        if sh['child'] == table_name:
            return sh
    
    return None


def discover_table_patterns(
    table_name: str,
    patterns_file: str = "src2/memory/patterns.json"
) -> Dict[str, Any]:
    """
    Discover all pattern information for a specific table
    
    Args:
        table_name: Name of the table to analyze
        patterns_file: Path to patterns JSON file
        
    Returns:
        Dictionary containing:
        - table_name: Name of the table
        - pattern_type: Main pattern type (SE, SEw, SR, SRR, SE_SH)
        - pattern_definition: Full definition of the main pattern
        - is_subclass: Boolean indicating if table is in SH relation
        - sh_info: Subclass relation details (if applicable)
        - sh_definition: SH pattern definition (if applicable)
    """
    print(f"Discovering patterns for table: {table_name}")
    
    # Load patterns
    patterns = load_patterns(patterns_file)
    
    # Get pattern type
    pattern_type = get_table_pattern_type(table_name, patterns)
    
    if not pattern_type:
        return {
            'table_name': table_name,
            'error': 'Table not found in patterns',
            'pattern_type': None,
            'pattern_definition': None,
            'is_subclass': False
        }
    
    # Get base pattern (SE, SEw, SR, or SRR)
    is_subclass = '_SH' in pattern_type
    base_pattern_type = pattern_type.replace('_SH', '') if is_subclass else pattern_type
    
    # Get pattern definition
    pattern_definition = get_pattern_definition(base_pattern_type, patterns)
    
    result = {
        'table_name': table_name,
        'pattern_type': base_pattern_type,
        'pattern_definition': pattern_definition,
        'is_subclass': is_subclass
    }
    
    # Add SH information if applicable
    if is_subclass:
        sh_info = get_sh_relation_info(table_name, patterns)
        sh_definition = get_pattern_definition('SH', patterns)
        
        result['sh_info'] = sh_info
        result['sh_definition'] = sh_definition
    else:
        result['sh_info'] = None
        result['sh_definition'] = None
    
    print(f"✓ Pattern type: {base_pattern_type}")
    if is_subclass:
        print(f"✓ Is subclass: Yes (child of {result['sh_info']['parent']})")
    
    return result


def get_all_tables_by_pattern(
    pattern_type: str,
    patterns_file: str = "src2/memory/patterns.json"
) -> List[str]:
    """
    Get all tables of a specific pattern type
    
    Args:
        pattern_type: Pattern type to filter by (SE, SEw, SR, SRR, SH)
        patterns_file: Path to patterns JSON file
        
    Returns:
        List of table names matching the pattern type
    """
    patterns = load_patterns(patterns_file)
    
    # Map pattern type to the list key
    pattern_map = {
        'SE': 'SE_tables',
        'SEw': 'SEw_tables',
        'SR': 'SR_tables',
        'SRR': 'SRR_tables'
    }
    
    if pattern_type in pattern_map:
        return patterns.get(pattern_map[pattern_type], [])
    elif pattern_type == 'SH':
        # For SH, return all children from SH_relations
        sh_relations = patterns.get('SH_relations', [])
        return list(set(sh['child'] for sh in sh_relations))
    else:
        return []


def format_pattern_for_llm(pattern_info: Dict[str, Any]) -> str:
    """
    Format pattern information as text for LLM consumption
    
    Args:
        pattern_info: Pattern information dictionary
        
    Returns:
        Formatted string for LLM
    """
    if pattern_info.get('error'):
        return f"Error: {pattern_info['error']}"
    
    table_name = pattern_info['table_name']
    pattern_type = pattern_info['pattern_type']
    pattern_def = pattern_info['pattern_definition']
    
    text = f"TABLE: {table_name}\n"
    text += f"PATTERN TYPE: {pattern_type} ({pattern_def['name']})\n\n"
    text += f"MEANING: {pattern_def['meaning']}\n\n"
    text += f"MAPPING STRATEGY: {pattern_def['mapping_strategy']}\n\n"
    text += f"ATTRIBUTES HANDLING: {pattern_def['attributes_handling']}\n"
    
    if pattern_info['is_subclass']:
        sh_info = pattern_info['sh_info']
        sh_def = pattern_info['sh_definition']
        text += f"\n{'='*60}\n"
        text += f"ADDITIONAL PATTERN: Subclass Hierarchy\n\n"
        text += f"This table is a SUBCLASS of: {sh_info['parent']}\n"
        text += f"Inheritance via column: {sh_info['pk_fk_column']} → {sh_info['parent']}.{sh_info['parent_pk_column']}\n\n"
        text += f"SH MEANING: {sh_def['meaning']}\n\n"
        text += f"SH MAPPING STRATEGY: {sh_def['mapping_strategy']}\n\n"
        text += f"SH ATTRIBUTES HANDLING: {sh_def['attributes_handling']}\n"
    
    return text


if __name__ == "__main__":
    # Example usage
    
    # Example 1: Discover pattern for a specific table
    table_name = "papers"
    
    print("="*60)
    print("EXAMPLE 1: Discover pattern for 'papers' table")
    print("="*60)
    
    pattern_info = discover_table_patterns(table_name)
    
    print("\n" + "="*60)
    print("PATTERN INFO (JSON)")
    print("="*60)
    print(json.dumps(pattern_info, indent=2))
    
    print("\n" + "="*60)
    print("PATTERN INFO (LLM FORMAT)")
    print("="*60)
    print(format_pattern_for_llm(pattern_info))
    
