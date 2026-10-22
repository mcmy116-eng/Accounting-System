"""Run the single authorised import on the target Render service only."""
import os

from app.sample_data import TARGET_SERVICE


def main():
    # Other installations of this repository must never receive this sample.
    if os.environ.get('RENDER_SERVICE_ID') != TARGET_SERVICE:
        print('Dummy/testing: not applicable to this service; no data written.')
        return
    from app import create_app
    from app.sample_data import install_dummy_data
    app = create_app()
    with app.app_context():
        try:
            result = install_dummy_data(os.environ['RENDER_SERVICE_ID'])
            print('Dummy/testing:', result['status'],
                  'already recorded' if result.get('repeated') else result.get('reason', '13 journals; 13 evidence files; readback verified.'))
        except Exception as exc:
            # A sample-data failure must not take down the accounting website.
            # Do not log DB parameters, credentials or the private data payload.
            print('Dummy/testing: import failed (' + type(exc).__name__ + '); review service storage/database access before retrying.')


if __name__ == '__main__':
    main()
