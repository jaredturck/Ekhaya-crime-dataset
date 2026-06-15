from django.contrib import admin

from .models import (
    AreaPrecinctMatch,
    CrimeArea,
    CrimeImportRun,
    DataSource,
    ImportWarning,
    PlaceAlias,
    SapsCrimeMetric,
    SapsPrecinct,
)

@admin.register(DataSource)
class DataSourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'source_file_name', 'source_url', 'imported_at')
    search_fields = ('name', 'source_file_name', 'source_url')
    readonly_fields = ('imported_at',)

@admin.register(CrimeImportRun)
class CrimeImportRunAdmin(admin.ModelAdmin):
    list_display = (
        'id',
        'status',
        'suburb_count',
        'precinct_count',
        'crime_metric_count',
        'area_precinct_match_count',
        'warning_count',
        'started_at',
        'completed_at',
    )
    list_filter = ('status',)
    search_fields = ('notes',)
    readonly_fields = ('started_at', 'completed_at')

@admin.register(CrimeArea)
class CrimeAreaAdmin(admin.ModelAdmin):
    list_display = ('area_name', 'area_type', 'municipality', 'province', 'source_object_id', 'import_run')
    list_filter = ('area_type', 'municipality', 'province', 'import_run')
    search_fields = ('area_name', 'normalized_name', 'source_object_id')
    autocomplete_fields = ('data_source', 'import_run')
    readonly_fields = ('created_at',)

@admin.register(SapsPrecinct)
class SapsPrecinctAdmin(admin.ModelAdmin):
    list_display = ('precinct_name', 'component_name', 'province', 'source_object_id', 'source_create_date', 'import_run')
    list_filter = ('province', 'import_run')
    search_fields = ('precinct_name', 'normalized_name', 'component_name', 'source_object_id')
    autocomplete_fields = ('data_source', 'import_run')
    readonly_fields = ('created_at',)

@admin.register(AreaPrecinctMatch)
class AreaPrecinctMatchAdmin(admin.ModelAdmin):
    list_display = ('crime_area', 'saps_precinct', 'overlap_percent', 'is_primary', 'match_method', 'import_run')
    list_filter = ('is_primary', 'match_method', 'import_run')
    search_fields = ('crime_area__area_name', 'saps_precinct__precinct_name', 'notes')
    autocomplete_fields = ('crime_area', 'saps_precinct', 'import_run')
    readonly_fields = ('created_at',)

@admin.register(SapsCrimeMetric)
class SapsCrimeMetricAdmin(admin.ModelAdmin):
    list_display = (
        'station_name',
        'crime_category',
        'period_name',
        'incidents',
        'district',
        'province',
        'saps_precinct',
        'import_run',
    )
    list_filter = ('province', 'district', 'period_name', 'crime_category', 'crime_group', 'import_run')
    search_fields = (
        'station_name',
        'normalized_station_name',
        'crime_category',
        'crime_group',
        'crime_code',
        'saps_precinct__precinct_name',
    )
    autocomplete_fields = ('data_source', 'import_run', 'saps_precinct')
    readonly_fields = ('created_at',)

@admin.register(PlaceAlias)
class PlaceAliasAdmin(admin.ModelAdmin):
    list_display = ('alias_name', 'crime_area', 'match_type', 'confidence', 'reviewed', 'created_at')
    list_filter = ('match_type', 'confidence', 'reviewed')
    search_fields = ('alias_name', 'normalized_alias', 'crime_area__area_name')
    autocomplete_fields = ('crime_area',)
    readonly_fields = ('created_at',)

@admin.register(ImportWarning)
class ImportWarningAdmin(admin.ModelAdmin):
    list_display = ('warning_type', 'source_file_name', 'source_row', 'import_run', 'created_at')
    list_filter = ('warning_type', 'source_file_name', 'import_run')
    search_fields = ('warning_type', 'message', 'source_file_name')
    autocomplete_fields = ('import_run',)
    readonly_fields = ('created_at',)
